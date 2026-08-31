from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any

from .models import (
    EvaluationResponse,
    PostEvaluationRequest,
    PrecheckRequest,
    PrecheckResponse,
    Q40BatchEvaluationRequest,
    Q40BatchResult,
)


class IdempotencyConflictError(ValueError):
    pass


class AgentStore:
    def __init__(self, database_path: str | Path) -> None:
        self.database_path = str(database_path)
        # 三反馈会在多个工作线程中共享同一个存储实例。SQLite 的单连接即使
        # 设置 check_same_thread=False，也不能让无锁读取与事务写入并发交错。
        # 使用可重入锁统一保护读写，并允许周期查询在锁内执行索引回填。
        self._lock = RLock()
        if self.database_path != ":memory:":
            Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.database_path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._initialize()

    def _initialize(self) -> None:
        with self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS precheck_runs (
                    check_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    input_snapshot_hash TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    response_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE (tenant_id, request_id)
                );
                CREATE INDEX IF NOT EXISTS ix_precheck_tenant_check
                    ON precheck_runs (tenant_id, check_id);

                CREATE TABLE IF NOT EXISTS evaluation_jobs (
                    job_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    input_snapshot_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    response_json TEXT,
                    error_message TEXT,
                    employee_id TEXT,
                    visit_date TEXT,
                    visit_record_code TEXT,
                    rule_version TEXT,
                    evaluation_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (tenant_id, request_id)
                );
                CREATE INDEX IF NOT EXISTS ix_evaluation_tenant_job
                    ON evaluation_jobs (tenant_id, job_id);
                CREATE TABLE IF NOT EXISTS q40_batch_jobs (
                    batch_job_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    input_snapshot_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    response_json TEXT,
                    error_message TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (tenant_id, request_id)
                );
                CREATE INDEX IF NOT EXISTS ix_q40_batch_tenant_job
                    ON q40_batch_jobs (tenant_id, batch_job_id);
                """
            )
            self._ensure_evaluation_columns()
            self._connection.execute(
                """
                CREATE INDEX IF NOT EXISTS ix_evaluation_period
                ON evaluation_jobs (tenant_id, employee_id, visit_date, status)
                """
            )

    def _ensure_evaluation_columns(self) -> None:
        existing = {
            row["name"]
            for row in self._connection.execute("PRAGMA table_info(evaluation_jobs)").fetchall()
        }
        additions = {
            "employee_id": "TEXT",
            "visit_date": "TEXT",
            "visit_record_code": "TEXT",
            "rule_version": "TEXT",
            "evaluation_id": "TEXT",
        }
        for name, sql_type in additions.items():
            if name not in existing:
                self._connection.execute(
                    f"ALTER TABLE evaluation_jobs ADD COLUMN {name} {sql_type}"
                )

    def get_precheck_by_request(self, tenant_id: str, request_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM precheck_runs WHERE tenant_id = ? AND request_id = ?",
                (tenant_id, request_id),
            ).fetchone()
            return self._precheck_record(row) if row else None

    def get_precheck(self, tenant_id: str, check_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM precheck_runs WHERE tenant_id = ? AND check_id = ?",
                (tenant_id, check_id),
            ).fetchone()
            return self._precheck_record(row) if row else None

    def save_precheck(self, request: PrecheckRequest, response: PrecheckResponse) -> None:
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT * FROM precheck_runs WHERE tenant_id = ? AND request_id = ?",
                (request.context.tenant_id, request.context.request_id),
            ).fetchone()
            if row:
                existing = self._precheck_record(row)
                if existing["input_snapshot_hash"] != response.input_snapshot_hash:
                    raise IdempotencyConflictError("相同request_id对应了不同输入快照")
                return
            self._connection.execute(
                """
                INSERT INTO precheck_runs (
                    check_id, tenant_id, request_id, input_snapshot_hash,
                    request_json, response_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    response.check_id,
                    request.context.tenant_id,
                    request.context.request_id,
                    response.input_snapshot_hash,
                    request.model_dump_json(),
                    response.model_dump_json(),
                    datetime.now(UTC).isoformat(),
                ),
            )

    def create_evaluation_job(
        self, job_id: str, request: PostEvaluationRequest, input_snapshot_hash: str
    ) -> tuple[dict[str, Any], bool]:
        now = datetime.now(UTC).isoformat()
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT * FROM evaluation_jobs WHERE tenant_id = ? AND request_id = ?",
                (request.context.tenant_id, request.context.request_id),
            ).fetchone()
            if row:
                existing = self._evaluation_record(row)
                if existing["input_snapshot_hash"] != input_snapshot_hash:
                    raise IdempotencyConflictError("相同request_id对应了不同评价输入")
                return existing, False
            self._connection.execute(
                """
                INSERT INTO evaluation_jobs (
                    job_id, tenant_id, request_id, input_snapshot_hash, status,
                    request_json, employee_id, visit_date, visit_record_code,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    request.context.tenant_id,
                    request.context.request_id,
                    input_snapshot_hash,
                    request.model_dump_json(),
                    request.visit.employee_id,
                    request.visit.visit_date.isoformat(),
                    request.visit_record_code,
                    now,
                    now,
                ),
            )
            row = self._connection.execute(
                "SELECT * FROM evaluation_jobs WHERE tenant_id = ? AND job_id = ?",
                (request.context.tenant_id, job_id),
            ).fetchone()
            return self._evaluation_record(row), True

    def complete_evaluation(self, response: EvaluationResponse) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """
                UPDATE evaluation_jobs
                SET status = 'completed', response_json = ?, rule_version = ?,
                    evaluation_id = ?, updated_at = ?
                WHERE tenant_id = ? AND job_id = ?
                """,
                (
                    response.model_dump_json(),
                    response.rule_version,
                    response.evaluation_id,
                    datetime.now(UTC).isoformat(),
                    response.tenant_id,
                    response.job_id,
                ),
            )

    def fail_evaluation(self, tenant_id: str, job_id: str, message: str) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """
                UPDATE evaluation_jobs
                SET status = 'failed', error_message = ?, updated_at = ?
                WHERE tenant_id = ? AND job_id = ?
                """,
                (message, datetime.now(UTC).isoformat(), tenant_id, job_id),
            )

    def get_evaluation_by_request(self, tenant_id: str, request_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM evaluation_jobs WHERE tenant_id = ? AND request_id = ?",
                (tenant_id, request_id),
            ).fetchone()
            return self._evaluation_record(row) if row else None

    def get_evaluation(self, tenant_id: str, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM evaluation_jobs WHERE tenant_id = ? AND job_id = ?",
                (tenant_id, job_id),
            ).fetchone()
        return self._evaluation_record(row) if row else None

    def tenant_runtime_activity(self, tenant_id: str) -> dict[str, Any]:
        """Return redacted operational evidence for the admin status dashboard."""
        precheck_rows = self._connection.execute(
            """
            SELECT check_id, response_json, created_at FROM precheck_runs
            WHERE tenant_id = ? ORDER BY created_at DESC
            """,
            (tenant_id,),
        ).fetchall()
        evaluation_rows = self._connection.execute(
            """
            SELECT job_id, status, response_json, error_message, created_at, updated_at
            FROM evaluation_jobs WHERE tenant_id = ? ORDER BY updated_at DESC
            """,
            (tenant_id,),
        ).fetchall()

        latest_precheck = None
        if precheck_rows:
            row = precheck_rows[0]
            response = json.loads(row["response_json"])
            semantic = response.get("semantic_review") or {}
            latest_precheck = {
                "check_id": row["check_id"],
                "created_at": row["created_at"],
                "result_status": response.get("status"),
                "semantic_status": semantic.get("status"),
                "semantic_model": semantic.get("model"),
                "failure_reason": semantic.get("failure_reason"),
            }

        latest_evaluation = None
        writeback_succeeded_count = 0
        for index, row in enumerate(evaluation_rows):
            response = json.loads(row["response_json"]) if row["response_json"] else {}
            writeback = response.get("writeback") or {}
            if writeback.get("status") == "succeeded":
                writeback_succeeded_count += 1
            if index == 0:
                semantic = response.get("semantic_facts") or {}
                latest_evaluation = {
                    "job_id": row["job_id"],
                    "status": row["status"],
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                    "total_score": response.get("total_score"),
                    "q33_score": response.get("q33_score"),
                    "q34_score": response.get("q34_score"),
                    "semantic_model": semantic.get("model"),
                    "semantic_provider": semantic.get("provider"),
                    "failure_reason": semantic.get("failure_reason"),
                    "writeback_status": writeback.get("status"),
                    "writeback_error": writeback.get("error_message"),
                    "job_error": row["error_message"],
                }

        return {
            "precheck": {"total_count": len(precheck_rows), "latest": latest_precheck},
            "evaluation": {
                "total_count": len(evaluation_rows),
                "completed_count": sum(row["status"] == "completed" for row in evaluation_rows),
                "failed_count": sum(row["status"] == "failed" for row in evaluation_rows),
                "pending_count": sum(
                    row["status"] not in {"completed", "failed"} for row in evaluation_rows
                ),
                "writeback_succeeded_count": writeback_succeeded_count,
                "latest": latest_evaluation,
            },
        }

    def list_completed_evaluations(
        self,
        tenant_id: str,
        employee_id: str,
        period_start: str,
        period_end: str,
        rule_version: str,
    ) -> list[dict[str, Any]]:
        with self._lock:
            self._backfill_evaluation_index(tenant_id)
            rows = self._connection.execute(
                """
                SELECT * FROM evaluation_jobs
                WHERE tenant_id = ? AND employee_id = ?
                  AND visit_date >= ? AND visit_date <= ?
                  AND status = 'completed' AND rule_version = ?
                ORDER BY visit_date, updated_at
                """,
                (tenant_id, employee_id, period_start, period_end, rule_version),
            ).fetchall()
            records = [self._evaluation_record(row) for row in rows]
        latest: dict[str, dict[str, Any]] = {}
        for record in records:
            code = record["request"]["visit_record_code"]
            latest[code] = record
        return list(latest.values())

    def _backfill_evaluation_index(self, tenant_id: str) -> None:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT job_id, request_json, response_json FROM evaluation_jobs
                WHERE tenant_id = ? AND status = 'completed'
                  AND (employee_id IS NULL OR visit_date IS NULL OR rule_version IS NULL)
                """,
                (tenant_id,),
            ).fetchall()
        if not rows:
            return
        with self._lock, self._connection:
            for row in rows:
                request = PostEvaluationRequest.model_validate_json(row["request_json"])
                response = EvaluationResponse.model_validate_json(row["response_json"])
                self._connection.execute(
                    """
                    UPDATE evaluation_jobs
                    SET employee_id = ?, visit_date = ?, visit_record_code = ?,
                        rule_version = ?, evaluation_id = ?
                    WHERE tenant_id = ? AND job_id = ?
                    """,
                    (
                        request.visit.employee_id,
                        request.visit.visit_date.isoformat(),
                        request.visit_record_code,
                        response.rule_version,
                        response.evaluation_id,
                        tenant_id,
                        row["job_id"],
                    ),
                )

    def create_q40_batch_job(
        self,
        batch_job_id: str,
        request: Q40BatchEvaluationRequest,
        input_snapshot_hash: str,
    ) -> tuple[dict[str, Any], bool]:
        now = datetime.now(UTC).isoformat()
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT * FROM q40_batch_jobs WHERE tenant_id = ? AND request_id = ?",
                (request.tenant_id, request.request_id),
            ).fetchone()
            if row:
                existing = self._q40_batch_record(row)
                if existing["input_snapshot_hash"] != input_snapshot_hash:
                    raise IdempotencyConflictError("相同Q40批次request_id对应了不同输入")
                return existing, False
            self._connection.execute(
                """
                INSERT INTO q40_batch_jobs (
                    batch_job_id, tenant_id, request_id, input_snapshot_hash,
                    status, request_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'queued', ?, ?, ?)
                """,
                (
                    batch_job_id,
                    request.tenant_id,
                    request.request_id,
                    input_snapshot_hash,
                    request.model_dump_json(),
                    now,
                    now,
                ),
            )
            row = self._connection.execute(
                "SELECT * FROM q40_batch_jobs WHERE tenant_id = ? AND batch_job_id = ?",
                (request.tenant_id, batch_job_id),
            ).fetchone()
            return self._q40_batch_record(row), True

    def complete_q40_batch(self, response: Q40BatchResult) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """
                UPDATE q40_batch_jobs
                SET status = ?, response_json = ?, updated_at = ?
                WHERE tenant_id = ? AND batch_job_id = ?
                """,
                (
                    response.status,
                    response.model_dump_json(),
                    datetime.now(UTC).isoformat(),
                    response.tenant_id,
                    response.batch_job_id,
                ),
            )

    def fail_q40_batch(self, tenant_id: str, batch_job_id: str, message: str) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """
                UPDATE q40_batch_jobs
                SET status = 'failed', error_message = ?, updated_at = ?
                WHERE tenant_id = ? AND batch_job_id = ?
                """,
                (message, datetime.now(UTC).isoformat(), tenant_id, batch_job_id),
            )

    def get_q40_batch(self, tenant_id: str, batch_job_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM q40_batch_jobs WHERE tenant_id = ? AND batch_job_id = ?",
                (tenant_id, batch_job_id),
            ).fetchone()
            return self._q40_batch_record(row) if row else None

    @staticmethod
    def _precheck_record(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "check_id": row["check_id"],
            "tenant_id": row["tenant_id"],
            "request_id": row["request_id"],
            "input_snapshot_hash": row["input_snapshot_hash"],
            "request": json.loads(row["request_json"]),
            "response": json.loads(row["response_json"]),
            "created_at": row["created_at"],
        }

    @staticmethod
    def _evaluation_record(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "job_id": row["job_id"],
            "tenant_id": row["tenant_id"],
            "request_id": row["request_id"],
            "input_snapshot_hash": row["input_snapshot_hash"],
            "status": row["status"],
            "request": json.loads(row["request_json"]),
            "response": json.loads(row["response_json"]) if row["response_json"] else None,
            "error_message": row["error_message"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _q40_batch_record(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "batch_job_id": row["batch_job_id"],
            "tenant_id": row["tenant_id"],
            "request_id": row["request_id"],
            "input_snapshot_hash": row["input_snapshot_hash"],
            "status": row["status"],
            "request": json.loads(row["request_json"]),
            "response": json.loads(row["response_json"]) if row["response_json"] else None,
            "error_message": row["error_message"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
