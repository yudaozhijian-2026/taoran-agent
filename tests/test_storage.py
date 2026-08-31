import json
import sqlite3

from taoran_agent.storage import AgentStore


def test_existing_evaluation_table_is_migrated_without_replacing_data(tmp_path) -> None:
    database_path = tmp_path / "legacy.db"
    connection = sqlite3.connect(database_path)
    connection.executescript(
        """
        CREATE TABLE evaluation_jobs (
            job_id TEXT PRIMARY KEY,
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
        """
    )
    connection.execute(
        """
        INSERT INTO evaluation_jobs (
            job_id, tenant_id, request_id, input_snapshot_hash, status,
            request_json, created_at, updated_at
        ) VALUES ('job-old', 'tenant_demo', 'old-request', 'hash', 'queued', '{}', 'a', 'a')
        """
    )
    connection.commit()
    connection.close()

    store = AgentStore(database_path)
    columns = {
        row[1]
        for row in store._connection.execute("PRAGMA table_info(evaluation_jobs)").fetchall()
    }

    assert {"employee_id", "visit_date", "rule_version", "evaluation_id"} <= columns
    assert store.get_evaluation("tenant_demo", "job-old") is not None


def test_tenant_runtime_activity_returns_redacted_operational_evidence(tmp_path) -> None:
    store = AgentStore(tmp_path / "runtime.db")
    precheck_response = {
        "status": "passed",
        "semantic_review": {
            "status": "completed",
            "model": "glm-5.2",
            "failure_reason": None,
        },
    }
    evaluation_response = {
        "total_score": 88,
        "q33_score": 44,
        "q34_score": 44,
        "semantic_facts": {"model": "glm-5.2", "provider": "llm-chat"},
        "writeback": {"status": "succeeded", "written_fields": ["AI评分"]},
    }
    with store._connection:
        store._connection.execute(
            """
            INSERT INTO precheck_runs VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "check-1", "tenant-a", "request-1", "hash-1", "{}",
                json.dumps(precheck_response), "2026-08-31T01:00:00+00:00",
            ),
        )
        store._connection.execute(
            """
            INSERT INTO evaluation_jobs (
                job_id, tenant_id, request_id, input_snapshot_hash, status,
                request_json, response_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "job-1", "tenant-a", "request-2", "hash-2", "completed", "{}",
                json.dumps(evaluation_response), "2026-08-31T01:01:00+00:00",
                "2026-08-31T01:02:00+00:00",
            ),
        )

    result = store.tenant_runtime_activity("tenant-a")

    assert result["precheck"]["total_count"] == 1
    assert result["precheck"]["latest"]["semantic_model"] == "glm-5.2"
    assert result["evaluation"]["completed_count"] == 1
    assert result["evaluation"]["writeback_succeeded_count"] == 1
    assert result["evaluation"]["latest"]["total_score"] == 88
    assert "request" not in result["evaluation"]["latest"]
