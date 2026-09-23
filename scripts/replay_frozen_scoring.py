from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from taoran_agent import __version__
from taoran_agent.models import PostEvaluationRequest, Q34SemanticFacts
from taoran_agent.scoring import score_q33, score_q34


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()

    source_bytes = args.source.read_bytes()
    rows = json.loads(source_bytes)
    results = []
    for row in rows:
        request = PostEvaluationRequest.model_validate_json(row["request_json"])
        response = json.loads(row["response_json"])
        facts = Q34SemanticFacts.model_validate(response["semantic_facts"])
        q33, _ = score_q33(request.visit)
        q34, _ = score_q34(request.visit, facts)
        results.append(
            {
                "job_id": row["job_id"],
                "q33": q33.score,
                "q34": q34.score,
                "total": q33.score + q34.score,
            }
        )
    result = {
        "version": __version__,
        "source_sha256": hashlib.sha256(source_bytes).hexdigest(),
        "record_count": len(results),
        "records": sorted(results, key=lambda item: item["job_id"]),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
