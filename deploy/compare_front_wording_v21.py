"""Build the frozen Original/V2/V2.1 comparison without network or writeback."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from copy import deepcopy
from pathlib import Path
from statistics import mean

from taoran_agent.api import _quick_check_final_analysis
from taoran_agent.front_analysis_artifact import build_artifact
from taoran_agent.front_quick_check_wording_v2 import project
from taoran_agent.front_v46 import POLICY_VERSION
from taoran_agent.front_v46.decision_ledger import build, with_validated_analysis
from taoran_agent.models import Q34SemanticFacts, VisitDraftInput
from taoran_agent.scoring import score_q33, score_q34


def _suggestion_count(text: str) -> int:
    return len(re.findall(r"(?m)^\s*\d+[、.]", text))


def _long_echo(visit: dict, output: str) -> bool:
    source = "\n".join(
        str(visit.get(field) or "") for field in ("process_description", "customer_feedback")
    )
    sentences = [item.strip() for item in re.split(r"[。！？；;\n]+", source)]
    return any(len(item) >= 40 and item in output for item in sentences)


def _metrics(rows: list[dict], key: str) -> dict:
    texts = [row[key] for row in rows]
    return {
        "average_length": round(mean(map(len, texts)), 2),
        "average_suggestion_count": round(mean(map(_suggestion_count, texts)), 2),
        "record_highlight_template": sum("记录中的重点是" in text for text in texts),
        "long_raw_record_echo": sum(_long_echo(row["visit"], row[key]) for row in rows),
        "generic_existing_plan_template": sum("按已有计划" in text for text in texts),
        "self_assessment_without_conflict": sum("重新核对自评" in text for text in texts),
        "forced_quarter": sum(
            bool(
                re.search(
                    r"必须跨季度|确保跨季度|不跨季度即不通过|请调整到下一季度|"
                    r"按潜力客户要求安排在不同自然季度",
                    text,
                )
            )
            for text in texts
        ),
        "forced_month": sum(
            bool(
                re.search(
                    r"必须跨月|确保跨月|请调整到下一月|按目标客户要求安排在不同自然月",
                    text,
                )
            )
            for text in texts
        ),
        "unsupported_sales_action": sum(
            bool(re.search(r"准备报价|提供供货方案|推动采购|要求客户确认预算", text))
            for text in texts
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("frozen")
    parser.add_argument("original")
    parser.add_argument("v2")
    parser.add_argument("output")
    args = parser.parse_args()
    frozen = json.loads(Path(args.frozen).read_text())["records"]
    original = {
        row["data_id"]: row
        for row in json.loads(Path(args.original).read_text())["records"]
    }
    v2 = {row["data_id"]: row for row in json.loads(Path(args.v2).read_text())["records"]}
    rows = []
    for record in frozen:
        request = record["request"]
        visit = VisitDraftInput.model_validate(request["visit"])
        original_text = original[record["data_id"]]["front_feedback"]
        v2_text = v2[record["data_id"]]["after"]
        analysis = _quick_check_final_analysis(original_text)
        artifact = build_artifact(
            visit=visit,
            tenant_id=request["context"]["tenant_id"],
            user_id=request["context"]["user_id"],
            check_id="frozen-v21-" + record["data_id"],
            quick_check_input_hash="a" * 64,
            source_record_id=record["data_id"],
            feedback_text=original_text,
            decision_ledger=with_validated_analysis(build(visit), analysis),
            front_review=None,
            policy_version=POLICY_VERSION,
        )
        artifact_before = deepcopy(artifact.model_dump(mode="json"))
        semantic_facts = Q34SemanticFacts.model_validate(record["response"]["semantic_facts"])
        scores_before = [score_q33(visit)[0].score, score_q34(visit, semantic_facts)[0].score]
        view = project(
            visit,
            validated_analysis=analysis,
            findings=[item.model_dump(mode="json") for item in artifact.findings],
        )
        scores_after = [score_q33(visit)[0].score, score_q34(visit, semantic_facts)[0].score]
        evidence = view["analysis_basis"] + [
            item
            for suggestion in view["suggestions"]
            for item in suggestion["suggestion_basis"]["evidence"]
        ]
        grounded = all(
            str(item["quote"]) in str(request["visit"].get(item["field"]) or "")
            for item in evidence
        )
        rows.append(
            {
                "data_id": record["data_id"],
                "record_code": request["visit_record_code"],
                "input_sha256": hashlib.sha256(
                    json.dumps(request["visit"], sort_keys=True).encode()
                ).hexdigest(),
                "visit": request["visit"],
                "original": original_text,
                "v2": v2_text,
                "v21": view["feedback_text"],
                "view": view,
                "artifact_unchanged": artifact_before == artifact.model_dump(mode="json"),
                "evidence_exact_match": grounded,
                "scores_before": scores_before + [sum(scores_before)],
                "scores_after": scores_after + [sum(scores_after)],
                "scores_unchanged": scores_before == scores_after,
            }
        )
    assert len(rows) == 30
    agreed_codes = {
        "BFJL2026092000349",
        "BFJL2026092100363",
        "BFJL2026092100364",
    }
    event_only_codes = {
        "BFJL2026092100361",
        "BFJL2026092100362",
        "BFJL2026092100365",
        "BFJL2026092100366",
        "BFJL2026092100367",
        "BFJL2026092100368",
        "BFJL2026092100369",
    }
    missing_plan_rows = [
        row
        for row in rows
        if row["visit"].get("next_contact_at") is None
        and row["record_code"] not in agreed_codes | event_only_codes
        and row["view"]["contact_state"] == "unknown"
    ]
    summary = {
        "records": len(rows),
        "original": _metrics(rows, "original"),
        "v2": _metrics(rows, "v2"),
        "v21": _metrics(rows, "v21"),
        "confirmed_next_contact_missed": sum(
            row["view"]["contact_state"] != "agreed"
            for row in rows
            if row["record_code"] in agreed_codes
        ),
        "future_customer_event_misclassified": sum(
            row["view"]["contact_state"] == "agreed"
            for row in rows
            if row["record_code"] in event_only_codes
        ),
        "missing_plan_without_planning_advice": sum(
            "规划联系时间" not in row["v21"] for row in missing_plan_rows
        ),
        "artifact_unchanged": sum(row["artifact_unchanged"] for row in rows),
        "scores_unchanged": sum(row["scores_unchanged"] for row in rows),
        "evidence_exact_match": sum(row["evidence_exact_match"] for row in rows),
    }
    output = Path(args.output)
    output.write_text(json.dumps({"summary": summary, "records": rows}, ensure_ascii=False, indent=2))
    output.with_suffix(".md").write_text(
        "# 30条冻结输入 Original / V2 / V2.1 对比\n\n"
        + "\n\n".join(
            f"## {index}. {row['record_code']}\n\n"
            f"### Original Front\n\n{row['original']}\n\n"
            f"### V2 Front\n\n{row['v2']}\n\n"
            f"### V2.1 Front\n\n{row['v21']}"
            for index, row in enumerate(rows, 1)
        )
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
