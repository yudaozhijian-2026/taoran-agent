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
