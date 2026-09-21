"""Rerun frozen Front inputs only; never submit, acknowledge, or write back."""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from queue import Queue
from time import monotonic

from taoran_agent import __version__
from taoran_agent.api import _quick_check_run, get_store
from taoran_agent.config import get_settings
from taoran_agent.models import PrecheckRequest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("frozen")
    parser.add_argument("output")
    args = parser.parse_args()
    settings = get_settings()
    assert settings.environment == "isolated-submit-test"
    assert settings.submit_confirmation_enabled
    records = json.loads(Path(args.frozen).read_text())["records"]
    assert len(records) == 30
    previous = json.loads(Path(args.output).read_text())["records"] if Path(args.output).exists() else []
    result = [r for r in previous if r["outcome"]["final"].get("status") == "completed"]
    completed_ids = {r["data_id"] for r in result}
    todo = [r for r in records if r["data_id"] not in completed_ids]

    def run(record):
        raw = record["request"]
        request = PrecheckRequest.model_validate({"context": raw["context"], "visit": raw["visit"]})
        request.context.request_id = f"front-wording-{__version__}-{record['data_id']}"
        digest = hashlib.sha256(json.dumps(raw["visit"], sort_keys=True).encode()).hexdigest()
        events = Queue()
        start = monotonic()
        outcome = _quick_check_run(request, settings, events, check_id=request.context.request_id,
                                   quick_check_input_hash=digest)
        public_events = []
        while not events.empty():
            event = events.get()
            if event.get("type") in {"preview_replace", "suggestion_replace"}:
                public_events.append(event)
        artifact_id = outcome["final"].get("front_analysis_artifact_id")
        artifact = None
        if artifact_id:
            with sqlite3.connect("file:" + get_store(settings).database_path + "?mode=ro", uri=True) as conn:
                row = conn.execute("select payload_json from front_analysis_artifacts where artifact_id=?", (artifact_id,)).fetchone()
                artifact = json.loads(row[0]) if row else None
        return {"artifact": artifact, "data_id": record["data_id"], "record_code": raw["visit_record_code"],
                "input_sha256": digest, "seconds": round(monotonic()-start, 3),
                "events": public_events, "outcome": outcome}

    with ThreadPoolExecutor(max_workers=2) as pool:
        for future in as_completed([pool.submit(run, item) for item in todo]):
            item = future.result()
            result.append(item)
            Path(args.output).write_text(json.dumps({"version": __version__, "records": result}, ensure_ascii=False, indent=2))
            print(json.dumps({"count": len(result), "status": item["outcome"]["final"]["status"]}), flush=True)


if __name__ == "__main__":
    main()
