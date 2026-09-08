import json
from copy import deepcopy

import httpx
import pytest
from test_post_policy import visit

from taoran_agent.business_wording import business_wording
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
               '潜力客户不适用共识门槛，customer_consensus_met=true。next_action_logic_ok=true。')
    facts.sections = [ModelSectionAnalysis(code=c, verdict='met', reason='有记录', suggestion='', field_paths=[], evidence=[] )
                      for c in ['T', 'A1', 'O_KR', 'R', 'A2', 'N']]
    original = deepcopy(facts.model_dump())
    output = build_evaluation_feedback(visit(), 40, 20, 60, [], facts)
    assert 'key_result_quality_ok' not in output and '=true' not in output
    assert '不适用共识门槛' in output and 'LKXA' in output
    assert '客户已确认' not in output
    assert facts.model_dump() == original
    assert 'key_result_quality_ok' not in _clean_experimental_front_text(facts.reason)


def test_business_names_and_layout_survive():
    text = 'LKXA、5kg、BD、P2、API、product_code_X\n客户尚未批准预算。'
    assert business_wording(text) == text


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
