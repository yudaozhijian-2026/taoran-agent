"""Frozen 30-input presentation comparison; no network or writeback."""
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("frozen")
    parser.add_argument("before")
    parser.add_argument("output")
    args = parser.parse_args()
    frozen = json.loads(Path(args.frozen).read_text())["records"]
    old = {r["data_id"]: r for r in json.loads(Path(args.before).read_text())["records"]}
    rows = []
    for record in frozen:
        request = record["request"]
        visit = VisitDraftInput.model_validate(request["visit"])
        before = old[record["data_id"]]["front_feedback"]
        analysis = _quick_check_final_analysis(before)
        artifact = build_artifact(visit=visit, tenant_id=request["context"]["tenant_id"],
                                 user_id=request["context"]["user_id"], check_id="frozen-"+record["data_id"],
                                 quick_check_input_hash="a"*64, source_record_id=record["data_id"],
                                 feedback_text=before, decision_ledger=with_validated_analysis(build(visit), analysis),
                                 front_review=None, policy_version=POLICY_VERSION)
        artifact_before = deepcopy(artifact.model_dump(mode="json"))
        facts = Q34SemanticFacts.model_validate(record["response"]["semantic_facts"])
        scores_before = [score_q33(visit)[0].score, score_q34(visit, facts)[0].score]
        view = project(visit, validated_analysis=analysis,
                       findings=[item.model_dump(mode="json") for item in artifact.findings])
        scores_after = [score_q33(visit)[0].score, score_q34(visit, facts)[0].score]
        unchanged = artifact_before == artifact.model_dump(mode="json")
        evidence = view["analysis_basis"] + [e for s in view["suggestions"] for e in s["suggestion_basis"]["evidence"]]
        grounded = all(str(e["quote"]) in str(request["visit"].get(e["field"]) or "") for e in evidence)
        rows.append({"data_id": record["data_id"], "record_code": request["visit_record_code"],
                     "input_sha256": hashlib.sha256(json.dumps(request["visit"],sort_keys=True).encode()).hexdigest(),
                     "before": before, "after": view["feedback_text"], "view": view,
                     "artifact_unchanged": unchanged, "evidence_exact_match": grounded,
                     "scores_unchanged": scores_before == scores_after,
                     "scores_before": scores_before + [sum(scores_before)],
                     "scores_after": scores_after + [sum(scores_after)]})
    assert len(rows) == 30
    def metrics(key):
        texts = [r[key] for r in rows]
        return {"average_length": round(mean(map(len,texts)),2),
                "average_body_length": round(mean(len(re.sub(r"提交后，系统将自动生成正式评分和反馈意见。", "", t).strip()) for t in texts),2),
                "average_suggestion_count": round(mean(len(re.findall(r"(?m)^\s*\d+[、.]",t)) for t in texts),2),
                "goal_repetition_records": sum(bool(re.search(r"原目标为|原定目标",t)) for t in texts),
                "form_repetition_records": sum(bool(re.search(r"(?:客户类型为|拜访日期为|拜访方式为|微信沟通，已预约|下一步目的为)",t)) for t in texts),
                "self_agreement_records": sum(bool(re.search(r"自评.{0,12}一致",t)) for t in texts)}
    summary = {"records":len(rows), "before":metrics("before"), "after":metrics("after"),
               "artifact_unchanged":sum(r["artifact_unchanged"] for r in rows),
               "scores_unchanged":sum(r["scores_unchanged"] for r in rows),
               "evidence_exact_match":sum(r["evidence_exact_match"] for r in rows)}
    summary["length_reduction_percent"] = round((1-summary["after"]["average_length"]/summary["before"]["average_length"])*100,2)
    summary["body_length_reduction_percent"] = round((1-summary["after"]["average_body_length"]/summary["before"]["average_body_length"])*100,2)
    Path(args.output).write_text(json.dumps({"summary":summary,"records":rows}, ensure_ascii=False,indent=2))
    Path(args.output).with_suffix(".md").write_text("# 30条冻结输入 Front 展示对比\n\n" + "\n\n".join(
        f"## {i}. {r['record_code']}\n\n### 原反馈\n\n{r['before']}\n\n### V2\n\n{r['after']}"
        for i,r in enumerate(rows,1)))
    print(json.dumps(summary,ensure_ascii=False,indent=2))


if __name__ == "__main__":
    main()
