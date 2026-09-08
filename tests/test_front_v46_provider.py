import json
from copy import deepcopy
from types import SimpleNamespace

import httpx
import pytest

from taoran_agent import api
from taoran_agent.config import Settings
from taoran_agent.front_v46.experimental_front_repairs import (
    merge_patch,
    patch_schema,
    process_fragments,
    retry_scope,
)
from taoran_agent.front_v46.reviewer import FrontReviewer as ChatModelReviewer


def context():
    return {
        "previous_candidate": {
            "analysis_points": [
                {
                    "contract_id": "C_PROCESS",
                    "text": "销售等待客户审批。",
                    "proofs": [{"quote": "等待"}],
                },
                {
                    "contract_id": "C_PROCESS",
                    "text": "客户计划下月回复。",
                    "proofs": [{"quote": "计划"}],
                },
            ],
            "items": [
                {"code": "R", "suggestion": "保留"},
                {"code": "O_KR", "suggestion": "待修正"},
            ],
        },
        "rejection": {"semantic_issues": [{"target": "analysis:1"}, {"target": "suggestion:1"}]},
    }


def test_scope_resolves_audit_positions_and_preserves_every_untargeted_byte():
    ctx = context()
    before = deepcopy(ctx)
    scope = retry_scope(ctx)
    assert scope == {"analysis_indices": [1], "item_codes": ["O_KR"]}
    point = {**ctx["previous_candidate"]["analysis_points"][1], "text": "计划下月回复，尚未完成。"}
    result = merge_patch(
        ctx,
        {
            "analysis_updates": [{"index": 1, "point": point}],
            "item_updates": [{"code": "O_KR", "item": {"code": "O_KR", "suggestion": "修正"}}],
        },
        scope,
    )
    assert ctx == before
    assert result["analysis_points"][0] == before["previous_candidate"]["analysis_points"][0]
    assert result["items"][0] == before["previous_candidate"]["items"][0]
    assert result["analysis_points"][1]["proofs"] == point["proofs"]


@pytest.mark.parametrize("target", ["all", "analysis:99", "suggestion:99", "suggestion:UNKNOWN"])
def test_ambiguous_or_unknown_business_target_never_becomes_partial_repair(target):
    ctx = context()
    ctx["rejection"]["semantic_issues"].append({"target": target})
    assert retry_scope(ctx) is None


@pytest.mark.parametrize("kind", ["extra", "index", "contract", "code", "duplicate", "bool"])
def test_patch_cannot_change_scope(kind):
    ctx = context()
    scope = retry_scope(ctx)
    patch = {
        "analysis_updates": [
            {"index": 1, "point": ctx["previous_candidate"]["analysis_points"][1]}
        ],
        "item_updates": [{"code": "O_KR", "item": ctx["previous_candidate"]["items"][1]}],
    }
    patch = deepcopy(patch)
    if kind == "extra":
        patch["items"] = []
    if kind == "index":
        patch["analysis_updates"][0]["index"] = 0
    if kind == "contract":
        patch["analysis_updates"][0]["point"]["contract_id"] = "C_NEXT"
    if kind == "code":
        patch["item_updates"][0]["item"]["code"] = "R"
    if kind == "duplicate":
        patch["analysis_updates"] *= 2
    if kind == "bool":
        patch["analysis_updates"][0]["index"] = True
    with pytest.raises(ValueError):
        merge_patch(ctx, patch, scope)


def test_structure_grouping_is_metadata_only_and_empty_patch_arrays_have_valid_schema():
    points = context()["previous_candidate"]["analysis_points"]
    before = deepcopy(points)
    assert process_fragments(points)[0]["point_indices"] == [0, 1]
    assert points == before
    full = {
        "properties": {
            "analysis_points": {"items": {"type": "object"}},
            "items": {"items": {"type": "object"}},
        }
    }
    schema = patch_schema(full, {"analysis_indices": [1], "item_codes": []})
    assert '"enum": []' not in json.dumps(schema)
    assert schema["properties"]["item_updates"]["maxItems"] == 0


@pytest.mark.parametrize("mode", ["process_fragments", "business_patch", "bad_patch"])
def test_full_pipeline_semantic_findings_do_not_trigger_generation_repair(monkeypatch, mode):
    from front_provider_fixture import PIPELINE_CONTEXT, pipeline_provider

    calls = []
    requests = []
    valid = []
    ordinary = pipeline_provider(
        ["reject", "pass"] if mode != "process_fragments" else ["pass"], calls
    )

    def provider(request):
        body = json.loads(request.content)
        incoming = json.loads(body["messages"][1]["content"])
        if "candidate" in incoming:
            return ordinary(request)
        requests.append(body)
        last = json.loads(body["messages"][-1]["content"])
        if "scope" in last:
            calls.append("patch")
            assert last["scope"] == {"analysis_indices": [0], "item_codes": []}
            point = deepcopy(valid[0]["analysis_points"][0])
            if mode == "bad_patch":
                point["contract_id"] = "C_NEXT"
            payload = {"analysis_updates": [{"index": 0, "point": point}], "item_updates": []}
        else:
            response = ordinary(request)
            payload = json.loads(response.json()["choices"][0]["message"]["content"])
            payload["suggestion_status"] = "no_change_needed"
            payload["suggestion_reason"] = "需要补充记录信息"
            valid.append(deepcopy(payload))
            if mode == "process_fragments":
                point = deepcopy(payload["analysis_points"][0])
                point.update(
                    contract_id="C_PROCESS",
                    goal_id="",
                    claim_type="recorded_fact",
                    kind="customer_fact",
                )
                point["proofs"] = [
                    p for p in point["proofs"] if p["field"] == "process_description"
                ]
                payload["analysis_points"].extend([point, deepcopy(point)])
        return httpx.Response(
            200,
            json={
                "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(payload)}}]
            },
        )

    settings = Settings(
        _env_file=None,
        llm_model="glm-test",
        llm_api_url="https://example.test/chat",
        llm_api_key="test",
        frontend_model_format_retries=1,
    )
    reviewer = ChatModelReviewer(settings, None, transport=httpx.MockTransport(provider))
    monkeypatch.setattr(
        api,
        "get_store",
        lambda _: SimpleNamespace(
            get_feedback_artifact=lambda *a: None, save_feedback_artifact=lambda *a: a[-1]
        ),
    )
    monkeypatch.setattr(
        api,
        "_front_specificity_items",
        lambda *a: [{"code": "R", "source_fields": PIPELINE_CONTEXT}],
    )
    monkeypatch.setattr(api, "_cached_knowledge_wording", lambda _: None)
    monkeypatch.setattr(api, "_apply_knowledge_wording", lambda _, wording, *a, **k: wording)
    response = SimpleNamespace(
        knowledge_snapshot_hash="k",
        knowledge_references=["ref"],
        issues=[],
        tenant_id="t",
        input_snapshot_hash="i",
    )
    try:
        result = api._enhance_front_suggestions(
            response, reviewer, settings, 5, {"visit_snapshot": PIPELINE_CONTEXT}, experimental=True
        )
        assert result.status == "completed", result.model_dump()
        assert len(requests) == 1
        assert "patch" not in calls
        assert result.visit_analysis
        assert result.model_attempts[0]["experimental_semantic_audit"]["status"] == "observed"
    finally:
        reviewer.close()


def test_required_goal_slots_precede_optional_process_fragments_without_changing_state():
    from taoran_agent.experimental_business_semantic_state import build_business_state
    from taoran_agent.experimental_rendering_guidance import rendering_input
    from taoran_agent.front_v46.experimental_front_repairs import rendering_output_plan

    for context in [
        {
            "expected_key_result": "获得参与权",
            "process_description": "客户感兴趣，等待进一步评估。",
        },
        {
            "expected_key_result": "客户确认合同文本最终版本，并承诺在约定日期完成签署",
            "process_description": "给客户网上找商品",
            "next_action_expected_result": "1",
        },
    ]:
        contracts = rendering_input(build_business_state(context))["RENDERING_CONTRACTS"]
        before = deepcopy(contracts)
        plan = rendering_output_plan(contracts)
        assert contracts == before
        assert plan["required_contract_ids"] == [
            c["contract_id"] for c in contracts if c["required"]
        ]
        assert all(p["contract_id"] != "C_CONTEXT" for p in plan["first_points"])
        assert plan["omit_optional_background"]
        assert [p["contract_id"] for p in plan["first_points"]] == plan["required_contract_ids"]
