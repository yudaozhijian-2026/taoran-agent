"""Front remains advisory while sharing input facts with post evaluation."""
import json

from test_post_policy import visit
from test_post_repair import reviewer


def test_front_shares_contract_and_does_not_request_scores(tmp_path):
    r = reviewer(tmp_path)
    try:
        v = visit()
        front = r._input(v, precheck=True)
        post = r._input(v, precheck=False)
        assert front['_record_contract'] == post['_record_contract']
        messages = r._messages(front, True)
        data = json.loads(messages[1]['content'])
        assert data['authoritative_checks']['record_contract'] == front['_record_contract']
        assert '不得输出分数' in messages[0]['content']
        assert '补写原目标' not in messages[0]['content']
    finally:
        r.close()
