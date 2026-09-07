"""Synthetic model/audit transport; no customer data or live calls."""

import json

import httpx

from taoran_agent.experimental_record_state import build
from taoran_agent.experimental_semantic_audit import CHECKS


def wire_verdict(payload, context):
    """Mock-provider fixture for the new comparison protocol."""
    sources = build(context)["sources"]
    issues = []
    for item in payload["issues"]:
        source = next(
            s for s in sources if s["field"] == item["field"] and item["quote"] in s["quote"]
        )
        relation = "denial" if item["check"] == "consistency" else "unsupported"
        actor = source["actor_hint"]
        issues.append(
            {k: item[k] for k in ("check", "error_type", "target", "output_quote", "reason")}
            | {
                "source_ids": [source["id"]],
                "comparison": {
                    "source_actor": actor,
                    "candidate_actor": actor,
                    "source_state": "reported",
                    "candidate_state": "not_attained",
                    "relation": relation,
                    "examined_targets": [item["target"]],
                },
            }
        )
    return {"checks": payload["checks"], "issues": issues}


PIPELINE_CONTEXT = {
    "expected_key_result": "沟通审批进度",
    "process_description": "客户表示审批暂缓。",
}

PIPELINE_ANALYSIS = "客户表示审批暂缓。"


def pipeline_provider(audit_results, calls):
    def respond(request):
        body = json.loads(request.content)
        incoming = json.loads(body["messages"][1]["content"])
        if "candidate" in incoming:
            calls.append("audit")
            outcome = audit_results.pop(0)
            if outcome == "upstream":
                return httpx.Response(503, text="PRIVATE_PROVIDER_ERROR")
            payload = {"checks": {key: True for key in CHECKS}, "issues": []}
            if outcome == "reject":
                payload["checks"]["consistency"] = False
                payload["issues"] = [
                    {
                        "check": "consistency",
                        "field": "process_description",
                        "quote": PIPELINE_CONTEXT["process_description"],
                        "output_quote": PIPELINE_ANALYSIS,
                        "target": "analysis:0",
                        "error_type": "fact_denial",
                        "reason": "模拟事实冲突",
                    }
                ]
            assert incoming["candidate"]["units"][0]["text"] == PIPELINE_ANALYSIS
            assert incoming["candidate"]["units"][0]["kind"] == "objective_result"
            payload = wire_verdict(payload, incoming["record"])
        else:
            if calls == ["generate", "audit"]:
                evidence = json.loads(body["messages"][-1]["content"])["retry_evidence_data"]
                assert evidence["rejection"]["semantic_issues"][0]["target"] == "analysis:0"
                assert (
                    evidence["previous_candidate"]["analysis_points"][0]["text"]
                    == PIPELINE_ANALYSIS
                )
            calls.append("generate")
            payload = {
                "analysis_points": [
                    {
                        "kind": "customer_fact",
                        "text": PIPELINE_ANALYSIS,
                        "proofs": [
                            {
                                "field": "process_description",
                                "quote": PIPELINE_CONTEXT["process_description"],
                            }
                        ],
                    }
                ],
                "items": [
                    {
                        "code": "R",
                        "suggestion": "",
                        "present": ["customer_expression_action"],
                        "proofs": [
                            {
                                "features": ["customer_expression_action"],
                                "field": "process",
                                "quote": PIPELINE_ANALYSIS,
                            }
                        ],
                    }
                ],
            }
        if "RENDERING_CONTRACTS" in incoming:
            contract = incoming["RENDERING_CONTRACTS"][0]
            payload["analysis_points"][0].update(
                kind="objective_result",
                contract_id=contract["contract_id"],
                goal_id=contract["goal_id"],
                claim_type=contract["allowed_claim_types"][0],
                fact_ids=contract["supporting_fact_ids"],
            )
            payload["analysis_points"][0]["proofs"].append(
                {"field": "expected_key_result", "quote": PIPELINE_CONTEXT["expected_key_result"]}
            )
        return httpx.Response(
            200,
            json={
                "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(payload)}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            },
        )

    return respond
