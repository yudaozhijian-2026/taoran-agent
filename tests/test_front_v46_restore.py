import ast
import hashlib
import inspect
import json
import textwrap
from pathlib import Path

import pytest
from test_post_repair import reviewer

from taoran_agent import api
from taoran_agent.front_v46 import bind
from taoran_agent.front_v46.reviewer import FrontReviewer
from taoran_agent.llm import ChatModelReviewer


def test_front_policy_isolated_from_current_scoring(tmp_path):
    current = reviewer(tmp_path)
    try:
        before = current.review_q34.__func__
        front = bind(current)
        assert type(current) is ChatModelReviewer
        assert isinstance(front, FrontReviewer)
        assert current.review_q34.__func__ is before
        assert front._client is current._client
        assert front.model_capacity is current.model_capacity
        assert front.verbalize_knowledge_issues.__func__ is not current.verbalize_knowledge_issues.__func__
    finally:
        current.close()


def test_historical_front_methods_match_verified_v46_source():
    manifest = json.loads(Path("src/taoran_agent/front_v46/provenance.json").read_text())
    for name, digest in manifest["method_ast_sha256"].items():
        if name == "verbalize_knowledge_issues":
            continue  # Generation policy is now explicitly observation-only; audit remains V4.6.
        tree = ast.parse(textwrap.dedent(inspect.getsource(getattr(FrontReviewer, name))))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and "Independent candidate-only verifier" in node.value:
                node.value = inspect.cleandoc(node.value)
        assert hashlib.sha256(ast.dump(tree, include_attributes=False).encode()).hexdigest() == digest


@pytest.mark.parametrize("reason,count", [("timeout",3),("rate_limited",3),("wording_experimental_fact_conflict",1),("authentication_failed",1)])
def test_transient_retry_bounded_and_semantic_policy_not_retried_again(monkeypatch, reason, count):
    calls=[]
    monkeypatch.setattr(api,"_quick_check_run_final_once",lambda *a: calls.append(1) or {"status":"failed","diagnostics":{"failure_reason":reason}})
    monkeypatch.setattr("time.sleep",lambda _: None)
    result=api._quick_check_run_final(None,None)
    assert len(calls)==count and result["recoverable"]
