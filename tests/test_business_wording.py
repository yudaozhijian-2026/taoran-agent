import json
from copy import deepcopy

import httpx
import pytest
from test_post_policy import visit

from taoran_agent.business_wording import (
    BusinessWordingStream,
    business_wording,
    model_facing_visit_snapshot,
    normalize_generated_business_terms,
    normalize_generated_payload_wording,
    salesperson_feedback_hits,
    salesperson_wording,
)
from taoran_agent.config import Settings
from taoran_agent.feedback import _clean_experimental_front_text, build_evaluation_feedback
from taoran_agent.front_v46 import experimental_semantic_streaming_v22 as preview
from taoran_agent.models import ModelSectionAnalysis, Q34SemanticFacts


@pytest.mark.parametrize('key', ['key_result_quality_ok', 'process_fact_based',
                                'customer_consensus_met', 'next_action_logic_ok'])
@pytest.mark.parametrize('value', ['true', 'false'])
def test_internal_flags_become_business_wording(key, value):
    for notation in [f'{key}={value}', f'"{key}": {value}', f'facts.{key} == {value.upper()}']:
        output = business_wording(notation)
        assert key not in output
        assert value not in output.lower()
        assert output != business_wording(f'{key}={str(value == "false").lower()}')
        assert business_wording(output) == output


def test_formal_feedback_does_not_mutate_scoring_facts():
    facts = Q34SemanticFacts(provider='llm-test', key_result_quality_ok=False,
        process_fact_based=True, purpose_achievement='partially_achieved', next_action_logic_ok=True,
        customer_consensus_met=True,
        reason='原目标不够明确，key_result_quality_ok=false。推荐LKXA产品，process_fact_based=true。'
               '潜力客户不适用共识门槛，customer_consensus_met=true。'
               'N整体未满足时间门槛。next_action_logic_ok=true。')
    facts.sections = [ModelSectionAnalysis(code=c, verdict='met', reason='有记录', suggestion='', field_paths=[], evidence=[] )
                      for c in ['T', 'A1', 'O_KR', 'R', 'A2', 'N']]
    original = deepcopy(facts.model_dump())
    output = build_evaluation_feedback(visit(next_contact_at=None), 40, 20, 60, [], facts)
    assert 'key_result_quality_ok' not in output and '=true' not in output
    assert '不适用共识门槛' not in output and 'LKXA' in output
    assert '下一次联系客户时间安排尚未填写' in output
    assert not salesperson_feedback_hits(output)
    assert '客户已确认' not in output
    assert facts.model_dump() == original
    assert 'key_result_quality_ok' not in _clean_experimental_front_text(facts.reason)


def test_business_names_and_layout_survive():
    text = 'LKXA、5kg、BD、P2、API、product_code_X\n客户尚未批准预算。'
    assert business_wording(text) == text


@pytest.mark.parametrize("text", [
    "潜力客户不要求客户共识，共识视为满足。",
    "程序判定 period_met 为否。",
    "N整体不达标。",
    "时间门槛未满足。",
    "请说明跨季度要求不适用的依据。",
    "authoritative_checks.next_contact_policy显示为false。",
])
def test_salesperson_leak_detector_rejects_internal_rule_language(text):
    assert salesperson_feedback_hits(text)


def test_salesperson_wording_uses_recorded_date_fact_and_hides_exemption():
    text = (
        "下一步保持联系，但N整体未满足时间门槛。"
        "潜力客户无客户共识要求，共识视为满足。"
    )
    output = salesperson_wording(text, {
        "customer_type_ii": "潜力客户",
        "visit_date": "2026-09-17",
        "next_contact_at": None,
    })
    assert output == "下一步保持联系，但下一次联系客户时间安排尚未填写。"
    assert not salesperson_feedback_hits(output)


def test_salesperson_wording_describes_same_quarter_without_threshold_terms():
    output = salesperson_wording("程序判定 period_met 为否。", {
        "customer_type_ii": "潜力客户",
        "visit_date": "2026-09-17",
        "next_contact_at": "2026-09-30",
    })
    assert output == "填写的下一次联系日期与本次拜访仍在同一自然季度。"
    assert not salesperson_feedback_hits(output)


def test_salesperson_wording_removes_exception_explanation_request():
    output = salesperson_wording(
        "请说明跨季度要求不适用的依据。另请补充下一次联系客户时间安排。",
        {"customer_type_ii": "潜力客户", "visit_date": "2026-09-17"},
    )
    assert "不适用" not in output and "依据" not in output
    assert "补充下一次联系客户时间安排" in output
    assert not salesperson_feedback_hits(output)


@pytest.mark.parametrize("text", [
    "潜力客户不要求客户共识，共识视为满足。",
    "程序判定 period_met 为否。",
    "N整体不达标。",
    "时间门槛未满足。",
])
def test_interactive_preview_rejects_internal_rule_narration(text):
    snapshot = {
        "customer_type_ii": "潜力客户",
        "visit_date": "2026-09-17",
        "next_contact_at": "2026-10-01",
    }
    assert preview._interactive_preview_safe(text, snapshot) is False


def test_model_facing_visit_snapshot_uses_exact_form_options():
    source = {
        "customer_type_ii": "potential",
        "visit_method": "asynchronous_message",
        "is_appointment": False,
        "self_assessment": "partially_achieved",
        "process_description": "客户通过微信反馈。",
    }
    result = model_facing_visit_snapshot(source)
    assert result == {
        "customer_type_ii": "潜力客户",
        "visit_method": "微信/邮件/QQ沟通",
        "is_appointment": "未预约",
        "self_assessment": "部分达到目的",
        "process_description": "客户通过微信反馈。",
    }
    assert source["customer_type_ii"] == "potential"


@pytest.mark.parametrize("alias", [
    "潜在客户", "潜在型客户", "目标型客户", "机会客户", "商机型客户",
])
def test_customer_type_aliases_are_normalized(alias):
    result = normalize_generated_business_terms(f"本次对象为{alias}。")
    assert alias not in result
    assert any(label in result for label in ("潜力客户", "目标客户", "商机客户"))


@pytest.mark.parametrize("alias", [
    "异步沟通", "异步交流", "异步拜访", "异步消息沟通", "线上文字沟通", "即时通讯沟通",
])
def test_visit_method_aliases_use_actual_form_option(alias):
    result = normalize_generated_business_terms(
        f"本次采用{alias}。", {"visit_method": "微信/邮件/QQ沟通"},
    )
    assert result == "本次采用微信/邮件/QQ沟通。"


def test_unknown_visit_method_does_not_guess_alias_meaning():
    assert normalize_generated_business_terms("本次采用线上沟通。", {}) == "本次采用线上沟通。"


def test_payload_wording_changes_only_model_text_not_source_quotes():
    raw = {
        "analysis_points": [{
            "text": "潜在客户采用异步沟通。",
            "proofs": [{"field": "process_description", "quote": "潜在客户采用异步沟通"}],
        }],
        "items": [{"suggestion": "请完善异步沟通的结果。", "proofs": []}],
        "confirmations": [],
        "suggestion_reason": "异步沟通结果不清楚。",
    }
    result = normalize_generated_payload_wording(raw, {
        "customer_type_ii": "潜力客户", "visit_method": "微信/邮件/QQ沟通",
    })
    assert result["analysis_points"][0]["text"] == "潜力客户采用微信/邮件/QQ沟通。"
    assert result["items"][0]["suggestion"] == "请完善微信/邮件/QQ沟通的结果。"
    assert result["analysis_points"][0]["proofs"][0]["quote"] == "潜在客户采用异步沟通"
    assert raw["analysis_points"][0]["text"] == "潜在客户采用异步沟通。"


def test_stream_normalizes_aliases_split_across_chunks():
    output = []
    stream = BusinessWordingStream(output.append, {
        "visit_method": "微信/邮件/QQ沟通",
    })
    for chunk in ("本次为潜", "在客户，采用异", "步沟", "通。"):
        stream.feed(chunk)
    stream.flush()
    assert "".join(output) == "本次为潜力客户，采用微信/邮件/QQ沟通。"


def test_stream_fragments_do_not_expose_internal_flags(tmp_path, monkeypatch):
    settings = Settings(_env_file=None, database_path=str(tmp_path/'db'), llm_enabled=True,
        llm_model='test', llm_api_url='https://example.test/chat', llm_api_key='test')
    chunks = ['原目标比较宽泛，key_result_', 'quality_ok=', 'false。',
              '过程记录推荐LKXA产品，process_fact_', 'based=true。']
    def provider(request):
        events = [{'choices': [{'delta': {'content': c}}]} for c in chunks]
        events.append({'choices': [{'delta': {}, 'finish_reason': 'stop'}]})
        return httpx.Response(200, text=''.join('data: '+json.dumps(e)+'\n\n' for e in events))
    real = httpx.Client
    monkeypatch.setattr(preview.httpx, 'Client', lambda **k: real(transport=httpx.MockTransport(provider)))
    emitted = []
    result = preview.stream_semantic_preview_v22(settings, visit(), emitted.append, interactive=True)
    assert result['status'] == 'completed'
    assert emitted and all('key_result_' not in t and 'process_fact_' not in t for t in emitted)
    assert 'LKXA' in ''.join(emitted)
