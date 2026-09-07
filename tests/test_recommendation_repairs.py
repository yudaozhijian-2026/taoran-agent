import hashlib
import json
from copy import deepcopy

import httpx
import pytest

from taoran_agent import experimental_semantic_streaming_v22 as streaming
from taoran_agent.config import Settings
from taoran_agent.feedback import build_front_ai_suggestions_with_model
from taoran_agent.models import KnowledgeWordingItem, KnowledgeWordingResult, VisitDraftInput
from taoran_agent.recommendation_repairs import repair_preview_text, repair_r_recommendation


def record(index):
    from test_post_policy import visit

    data = visit(next_contact_at="2026-09-24", customer_feedback="").model_dump(mode="json")
    if index == 5:
        data.update(process_description="收集信息", expected_key_result="了解客户情况")
    elif index == 9:
        data.update(
            expected_key_result="客户确认合同文本最终版本，并承诺在约定日期完成签署",
            process_description="客户申请已提交，本月内点单付款。",
        )
    elif index == 10:
        data.update(process_description="简单与客户沟通后约定下次拜访。")
    return data


@pytest.mark.parametrize(
    "text",
    [
        "并约定9月24日下次联系。",
        "双方已约定9月24日下次联系。",
        "客户已约定于2026-09-24再次联系。",
    ],
)
def test_plan_date_is_reworded_once_without_rejection(text):
    source = {**record(5), "next_contact_at": "2026-09-24"}
    before = deepcopy(source)
    fixed, notes = repair_preview_text(text, source)
    assert "约定" not in fixed and "计划于" in fixed and notes
    assert repair_preview_text(fixed, source) == (fixed, [])
    assert source == before


@pytest.mark.parametrize(
    "text",
    [
        "建议双方约定9月24日下次联系。",
        "尚未约定9月24日下次联系。",
        "双方没有约定9月24日下次联系。",
        "计划9月24日再次联系。",
        "双方已约定9月25日下次联系。",
    ],
)
def test_uncertain_recommendations_and_other_dates_are_not_guessed(text):
    assert repair_preview_text(text, {**record(5), "next_contact_at": "2026-09-24"}) == (text, [])


def test_actual_agreement_is_preserved_and_date_without_agreement_is_separated():
    source = {**record(10), "next_contact_at": "2026-09-24"}
    text = "双方已约定9月24日下次联系。"
    fixed, notes = repair_preview_text(text, source)
    assert notes and "已形成" in fixed and "计划于9月24日" in fixed
    source["process_description"] = "双方约定9月24日下次联系。"
    assert repair_preview_text(text, source) == (text, [])
    text = "简单与客户沟通后约定下次拜访。"
    assert repair_preview_text(text, record(10)) == (text, [])


@pytest.mark.parametrize("chunk_size", [1, 7, 999])
def test_stream_repairs_before_display_preserving_success_and_hash(monkeypatch, chunk_size):
    text = (
        "本次拜访已记录开展收集信息动作，并约定9月24日下次联系。当前目标较宽泛，建议明确收集内容。"
    )
    wire = "<USER_FEEDBACK>" + text + "</USER_FEEDBACK>"
    lines = [
        "data: "
        + json.dumps(
            {"choices": [{"delta": {"content": wire[i : i + chunk_size]}}]}, ensure_ascii=False
        )
        + "\n\n"
        for i in range(0, len(wire), chunk_size)
    ]
    client = httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content="".join(lines)))
    )
    monkeypatch.setattr(streaming.httpx, "Client", lambda **kwargs: client)
    pieces = []
    result = streaming.stream_semantic_preview_v22(
        Settings(
            _env_file=None,
            llm_enabled=True,
            llm_model="glm-test",
            llm_api_url="https://example.test",
            llm_api_key="test",
        ),
        VisitDraftInput.model_validate(record(5)),
        pieces.append,
        interactive=True,
    )
    display = "".join(pieces)
    assert result["status"] == "completed" and result["failure_category"] is None
    assert "约定9月24日" not in display and "计划于9月24日" in display
    assert result["feedback_hash"] == hashlib.sha256(display.encode()).hexdigest()
    assert len(result["recommendation_repairs"]) == 1


def bad_item():
    return KnowledgeWordingItem(
        code="R",
        specific=False,
        suggestion="过程记录了申请已提交和本月内点单付款，但尚未填写客户反馈，无法确认客户对合同确认和签署的具体回应。",
    )


def test_r_repair_preserves_specificity_evidence_other_items_and_scores():
    items = [
        bad_item(),
        KnowledgeWordingItem(code="N", specific=False, suggestion="补充联系日期。"),
    ]
    before = [x.model_dump() for x in items]
    fixed, notes = repair_r_recommendation(items, record(9))
    assert notes and "客户申请已提交" in fixed[0].suggestion
    assert (
        fixed[0].specific is False
        and fixed[0].features == items[0].features
        and fixed[0].evidence == items[0].evidence
    )
    assert fixed[1] == items[1] and [x.model_dump() for x in items] == before
    assert repair_r_recommendation(fixed, record(9)) == (fixed, [])


@pytest.mark.parametrize(
    "process",
    [
        "给客户网上找商品",
        "客户计划提交申请",
        "客户申请尚未提交",
        "客户申请已提交，我认为项目肯定能推进。",
    ],
)
def test_genuine_process_gaps_and_unsupported_opinions_remain(process):
    items = [bad_item()]
    assert repair_r_recommendation(items, {**record(9), "process_description": process}) == (
        items,
        [],
    )


def test_render_moves_reminder_label_only_and_keeps_formal_branch_identical():
    from taoran_agent.agent import TaoranAgent
    from taoran_agent.models import PrecheckRequest

    structured = TaoranAgent().precheck(
        PrecheckRequest.model_validate(
            {
                "context": {
                    "tenant_id": "test",
                    "user_id": "u",
                    "request_id": "r",
                    "source": "test",
                },
                "visit": record(9),
            }
        )
    )
    before = structured.model_dump()
    fixed, notes = repair_r_recommendation([bad_item()], record(9))
    wording = KnowledgeWordingResult(
        status="completed",
        items=fixed,
        visit_analysis="当前记录尚未体现客户确认合同最终版本及承诺签署。",
        model_attempts=[{"recommendation_repairs": notes}],
    )
    text = build_front_ai_suggestions_with_model(structured, wording, experimental=True)
    assert "目标核对建议：" in text and "过程详细描述不具体" not in text
    assert structured.model_dump() == before
    formal = build_front_ai_suggestions_with_model(structured, wording, experimental=False)
    assert "目标核对建议：" not in formal and "过程详细描述不具体" in formal


def test_wording_repair_survives_existing_scoped_retry_without_extra_model_call(
    monkeypatch, tmp_path
):
    from types import SimpleNamespace

    from taoran_agent import api
    from taoran_agent.llm import ChatModelReviewer, ModelCallError

    context = record(9)
    calls = []
    audited = []
    points = []
    for index, text in [
        (1, "当前记录尚未体现客户确认合同文本最终版本。"),
        (2, "当前记录尚未体现客户承诺在约定日期完成签署。"),
    ]:
        points.append(
            {
                "contract_id": f"C_G{index}",
                "goal_id": f"G{index}",
                "claim_type": "unresolved",
                "kind": "objective_result",
                "text": text,
                "proofs": [
                    {"field": "expected_key_result", "quote": context["expected_key_result"]},
                    {"field": "process_description", "quote": context["process_description"]},
                ],
            }
        )
    points.append(
        {
            "contract_id": "C_PROCESS",
            "claim_type": "recorded_fact",
            "kind": "customer_fact",
            "text": "记录提到客户申请已提交，本月内点单付款。",
            "proofs": [{"field": "process_description", "quote": context["process_description"]}],
        }
    )
    payload = {
        "analysis_points": points,
        "items": [{"code": "R", "suggestion": bad_item().suggestion, "present": [], "proofs": []}],
    }

    def provider(request):
        body = json.loads(request.content)
        last = json.loads(body["messages"][-1]["content"])
        if "scope" in last:
            calls.append("scoped")
            assert last["scope"] == {"analysis_indices": [0], "item_codes": []}
            result = {"analysis_updates": [{"index": 0, "point": points[0]}], "item_updates": []}
        else:
            calls.append("initial")
            result = payload
        return httpx.Response(
            200,
            json={
                "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(result)}}]
            },
        )

    settings = Settings(
        _env_file=None,
        llm_enabled=True,
        llm_model="glm-test",
        llm_api_url="https://example.test",
        llm_api_key="test",
        database_path=str(tmp_path / "test.sqlite"),
    )
    reviewer = ChatModelReviewer(settings, None, transport=httpx.MockTransport(provider))

    def audit(context, analysis, suggestions, timeout, out, **kwargs):
        audited.append(suggestions)
        if len(audited) == 1:
            kwargs["repair_details"]["semantic_issues"] = [{"target": "analysis:0"}]
            raise ModelCallError("wording_experimental_audit_consistency")
        out.update(status="passed")

    monkeypatch.setattr(reviewer, "_experimental_audit_wording", audit)
    monkeypatch.setattr(
        api,
        "get_store",
        lambda _: SimpleNamespace(
            get_feedback_artifact=lambda *a: None, save_feedback_artifact=lambda *a: a[-1]
        ),
    )
    monkeypatch.setattr(
        api, "_front_specificity_items", lambda *a: [{"code": "R", "source_fields": context}]
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
            response, reviewer, settings, 5, {"visit_snapshot": context}, experimental=True
        )
        assert result.status == "completed", result.model_dump()
        assert calls == ["initial", "scoped"]
        assert len(audited) == 2 and audited[0] == audited[1]
        assert "过程已记录" in audited[0][0]
        assert result.model_attempts[-1]["recommendation_repairs"][0]["code"] == "R"
        assert result.items[0].specific is False
    finally:
        reviewer.close()
