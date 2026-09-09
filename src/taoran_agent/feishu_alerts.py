"""Durable failure notifications; no model calls, form writes or business gating."""
import base64
import hashlib
import hmac
import json
import logging
import time
from datetime import UTC, datetime
from threading import Event, Thread

import httpx

log = logging.getLogger(__name__)


def failures(kind, item):
    """Inspect terminal execution states only, never the meaning of model text."""
    if kind == "frontend":
        outcome = item.get("outcome") or {}
        final = outcome.get("final") or {}
        if item.get("status") == "failed":
            return ["AI最终反馈生成失败"]
        if item.get("status") == "completed":
            problems = []
            if item.get("return_failure"):
                problems.append("点击已读并返回修改后，最终反馈交付失败")
            if not str(final.get("feedback_text") or "").strip():
                problems.append("AI最终反馈正文为空")
            preview = outcome.get("preview") or {}
            if preview.get("status") in {"failed", "unavailable"}:
                problems.append("AI实时分析未生成，最终反馈单独核验")
            return problems
        return []
    if item["status"] == "failed":
        return ["后台分析或评分任务失败"]
    if item["status"] != "completed":
        return []
    result = json.loads(item.get("response_json") or "{}")
    request = json.loads(item["request_json"])
    problems = []
    if (result.get("semantic_facts") or {}).get("status") != "completed":
        problems.append("后台AI分析未完成")
    if not str(result.get("ai_opinion") or "").strip():
        problems.append("后台AI反馈意见未生成")
    if result.get("total_score") is None:
        problems.append("正式评分未生成")
    # Requests that never requested a writeback are not delivery failures.
    if request.get("writeback_target") and (result.get("writeback") or {}).get("status") != "succeeded":
        problems.append("反馈意见或评分未成功回写并核验")
    return problems


class AlertMonitor:
    def __init__(self, settings, store, client=None):
        self.settings, self.store = settings, store
        self.client = client
        self.stop_event = Event()
        self.thread = None
        with store._lock, store._connection:
            store._connection.executescript("""
                CREATE TABLE IF NOT EXISTS feishu_alert_seen (
                    event_key TEXT PRIMARY KEY
                );
                CREATE TABLE IF NOT EXISTS feishu_alert_meta (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS feishu_alert_outbox (
                    event_key TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,
                    task_id TEXT NOT NULL, payload_json TEXT NOT NULL,
                    status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                    next_at REAL NOT NULL DEFAULT 0, last_error TEXT,
                    created_at TEXT NOT NULL, sent_at TEXT
                );
            """)

    def scan(self):
        store = self.store
        with store._lock, store._connection:
            db = store._connection
            initialized = {row[0] for row in db.execute("SELECT key FROM feishu_alert_meta")}
            records = []
            for row in db.execute("SELECT payload_json FROM quick_check_recovery"):
                item = json.loads(row[0])
                if item.get("status") not in {"completed", "failed"}:
                    continue
                records.append(("frontend", item, item["tenant_id"], item["check_id"],
                                str(item.get("attempt", 0)),
                                item.get("record_code") or "未保存记录"))
            for row in db.execute("SELECT * FROM evaluation_jobs WHERE status IN ('completed','failed')"):
                item = dict(row)
                records.append(("backend", item, item["tenant_id"], item["job_id"],
                                item["updated_at"], item.get("visit_record_code") or "未提供"))
            for kind, item, tenant, task, revision, record in records:
                if tenant not in self.settings.feishu_alert_routes:
                    continue
                issues = failures(kind, item)
                key = hashlib.sha256(f'{tenant}|{kind}|{task}|{revision}|{issues}'.encode()).hexdigest()
                if db.execute("SELECT 1 FROM feishu_alert_seen WHERE event_key=?", (key,)).fetchone():
                    continue
                db.execute("INSERT INTO feishu_alert_seen VALUES (?)", (key,))
                if tenant not in initialized or not issues:
                    continue
                # Only routing identifiers and fixed business descriptions leave the service.
                # Never send original visit prose, model output, tokens, URLs or raw exceptions.
                content = '\n'.join([
                    'TAORAN异常通知', f'租户：{tenant}', f'拜访记录：{record}',
                    f'任务编号：{task}', '异常：' + '；'.join(issues),
                    '处理：前端失败请重新检测；后台失败请在管理页面查看任务并重新分析或重试回写。',
                    '说明：任务已保留；本通知不改变分析、评分或回写结果。',
                    '告警编号：' + key[:16],
                ])
                db.execute("""INSERT OR IGNORE INTO feishu_alert_outbox
                    (event_key,tenant_id,task_id,payload_json,status,created_at)
                    VALUES (?,?,?,?,'pending',?)""",
                           (key, tenant, task, json.dumps({'msg_type':'text', 'content':{'text':content}},
                                                        ensure_ascii=False), datetime.now(UTC).isoformat()))
            for tenant in self.settings.feishu_alert_routes:
                db.execute("INSERT OR IGNORE INTO feishu_alert_meta VALUES (?,'1')", (tenant,))

    def deliver(self):
        with self.store._lock, self.store._connection:
            self.store._connection.execute("""UPDATE feishu_alert_outbox SET status='failed',
                last_error='delivery_interrupted' WHERE status='pending' AND attempts>=3 AND next_at<=?""",
                (time.time(),))
            rows = self.store._connection.execute("""SELECT * FROM feishu_alert_outbox
                WHERE status='pending' AND attempts<3 AND next_at<=? ORDER BY created_at LIMIT 5""", (time.time(),)).fetchall()
        for row in rows:
            if self.stop_event.is_set():
                break
            route = self.settings.feishu_alert_routes.get(row['tenant_id'])
            if route is None:
                continue  # Never fall back to another tenant's group.
            count = row['attempts'] + 1
            with self.store._lock, self.store._connection:
                self.store._connection.execute("""UPDATE feishu_alert_outbox
                    SET attempts=?,next_at=? WHERE event_key=?""",
                    (count, time.time() + 30, row['event_key']))
            payload = json.loads(row['payload_json'])
            if route.signing_secret:
                stamp = str(int(time.time()))
                key = f'{stamp}\n{route.signing_secret.get_secret_value()}'.encode()
                payload.update(timestamp=stamp, sign=base64.b64encode(
                    hmac.new(key, b'', hashlib.sha256).digest()).decode())
            error = None
            try:
                client = self.client or httpx
                response = client.post(route.webhook_url.get_secret_value(), json=payload,
                                       timeout=5, follow_redirects=False)
                response.raise_for_status()
                result = response.json()
                if result.get('code', result.get('StatusCode')) != 0:
                    error = 'feishu_rejected'
            except Exception as exc:  # noqa: BLE001 - notifications never break business tasks
                error = type(exc).__name__  # No URLs/secrets/response body in logs or DB.
            status = 'sent' if error is None else 'failed' if count >= 3 else 'pending'
            with self.store._lock, self.store._connection:
                self.store._connection.execute("""UPDATE feishu_alert_outbox
                    SET status=?,attempts=?,next_at=?,last_error=?,sent_at=? WHERE event_key=?""",
                    (status, count, time.time() + (5 if count == 1 else 30), error,
                     datetime.now(UTC).isoformat() if status == 'sent' else None, row['event_key']))
            if error:
                log.warning('feishu_alert_delivery status=%s event=%s error=%s', status, row['event_key'][:16], error)

    def run(self):
        while not self.stop_event.is_set():
            try:
                self.scan()
                self.deliver()
            except Exception as exc:  # noqa: BLE001 - isolate worker/DB failures
                log.error('feishu_alert_worker error=%s', type(exc).__name__)
            self.stop_event.wait(self.settings.feishu_alert_poll_seconds)

    def start(self):
        self.scan()  # First enable baselines old terminal jobs; never floods historical tests.
        self.thread = Thread(target=self.run, name='taoran-feishu-alerts', daemon=True)
        self.thread.start()

    def close(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=6)


def start_monitor(settings, store):
    if not settings.feishu_alerts_enabled:
        return None
    if not settings.feishu_alert_routes:
        log.error('feishu_alerts enabled_without_routes')
        return None
    try:
        monitor = AlertMonitor(settings, store)
        monitor.start()
        return monitor
    except Exception as exc:  # noqa: BLE001 - service availability is independent
        log.error('feishu_alert_start error=%s', type(exc).__name__)
        return None


def notification_status(settings, store):
    """Operational counts only; never expose webhook credentials or visit data."""
    if not settings.feishu_alerts_enabled:
        return {'enabled': False}
    try:
        with store._lock:
            counts = dict(store._connection.execute(
                'SELECT status,count(*) FROM feishu_alert_outbox GROUP BY status').fetchall())
        return {'enabled':True, 'configured':bool(settings.feishu_alert_routes), **counts}
    except Exception:  # noqa: BLE001
        return {'enabled':True, 'available':False}
