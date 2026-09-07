import json
from copy import deepcopy
from threading import Event

import httpx
import pytest
from test_post_policy import visit

from taoran_agent.config import Settings
from taoran_agent.knowledge import load_taoran_knowledge_snapshot
from taoran_agent.llm import (
    ChatModelReviewer,
    ModelCallError,
    _evidence_catalog,
    _read_chat_response,
)
from taoran_agent.model_failure_evidence import save_failure_evidence
from taoran_agent.post_repair import merge_repair
from taoran_agent.post_review_policy import requirement_hits
from taoran_agent.post_trace import PostStreamTrace


def reviewer(tmp_path, **kwargs):
    return ChatModelReviewer(Settings(_env_file=None, database_path=str(tmp_path / "nested/db.sqlite"), **kwargs), load_taoran_knowledge_snapshot())


def valid_payload(r, v):
    catalog = _evidence_catalog(r._input(v, precheck=False))
    sections = []
    for code in ("T", "A1", "O_KR", "R", "A2", "N"):
        e = next(e for e in catalog if e["section"] == code)
        sections.append({"code": code, "verdict": "needs_revision", "reason": "本条记录尚需补充具体事实。",
            "advice_basis": {"fields":[e["field"]], "existing_content":"本次设备数量沟通", "missing_detail":"具体确认内容",
                             "decision_impact":"无法核验客户实际确认的设备数量", "gap_kind":"insufficient_specificity"},
            "suggestion": "请核实本次设备数量的客户反馈。", "field_paths": [e["field"]],
            "evidence": [{k: val for k, val in e.items() if k != "section"} | {"category": "system_fact"}]})
    return {"sections": sections, "facts": {"key_result_quality_ok": False, "process_fact_based": False,
        "purpose_achievement": "not_achieved", "next_action_logic_ok": False, "customer_consensus_met": False,
        "reason": "客户未提供设备数量。"}}


def test_directory_creation_and_private_evidence(tmp_path):
    s = Settings(_env_file=None, database_path=str(tmp_path / "a/b/db.sqlite"))
    ident = save_failure_evidence(s, stage="backend", candidate={"reason": "原始输出"}, details={"path": "sections.N.reason"})
    p = tmp_path / "a/b/model-failure-evidence" / (ident + ".json")
    assert json.loads(p.read_text())["candidate"]["reason"] == "原始输出"
    assert p.stat().st_mode & 0o777 == 0o600


def test_hits_locate_exact_text_without_global_role_rejection():
    text = "无需补充客户角色，但下一步行动对象必须填写具体联系人。"
    hits = requirement_hits(text, "target")
    assert len(hits) == 1
    assert text[hits[0]["start"]:hits[0]["end"]] == hits[0]["quote"]
    assert not requirement_hits("采购负责人身份未明确，本次目标是确认负责人，需核实", "target")
    assert not requirement_hits("下一步行动对象无需填写具体联系人", "target")


@pytest.mark.parametrize("error", ["requirement", "assessment", "field_reference"])
def test_real_validator_and_section_only_retry(tmp_path, monkeypatch, error):
    r = reviewer(tmp_path)
    v = visit()
    original = valid_payload(r, v)
    target = {"requirement":"N", "assessment":"A2", "field_reference":"T"}[error]
    index = {"N":5, "A2":4, "T":0}[target]
    if error == "requirement":
        original["sections"][index]["suggestion"] = "下一步行动对象必须填写具体联系人。"
    elif error == "assessment":
        # Self assessment is not_achieved; frozen actual achievement is partial.
        original["facts"]["purpose_achievement"] = "partially_achieved"
        original["sections"][index]["verdict"] = "met"
        original["sections"][index]["suggestion"] = ""
        # Nonzero achievement requires both KR and process evidence somewhere.
        catalog = _evidence_catalog(r._input(v, precheck=False))
        for section, field in ((2,"expected_key_result"),(3,"process_description")):
            e = next(e for e in catalog if e["section"] == original["sections"][section]["code"] and e["field"] == field)
            original["sections"][section]["evidence"] = [{k:x for k,x in e.items() if k != "section"} | {"category":"system_fact"}]
            original["sections"][section]["field_paths"] = [field]
    else:
        original["sections"][index]["field_paths"].append("other_purpose")
    repaired = deepcopy(original["sections"][index])
    repaired.update(verdict="needs_revision", suggestion="请根据已取得的设备信息校准本项。")
    if error == "field_reference":
        repaired["field_paths"].remove("other_purpose")
    calls = []
    def request(messages, precheck, timeout, lease, progress=None, repair=False):
        try:
            calls.append(repair)
            if repair:
                body = json.loads(messages[1]["content"])
                assert body["repair_targets"] == [target]
                assert body["validation_details"]
                return {"sections": [repaired], "facts_reason": ""}, {}
            return deepcopy(original), {}
        finally:
            lease.release()
    monkeypatch.setattr(r, "_request", request)
    attempts = []
    try:
        parsed, _ = r._analyze(v, False, attempts)
        assert calls == [False, True]
        assert parsed.facts.purpose_achievement == original["facts"]["purpose_achievement"]
        assert attempts[1].repair_targets == [target]
        assert attempts[0].diagnostic_evidence_id
        p = tmp_path / "nested/model-failure-evidence" / (attempts[0].diagnostic_evidence_id + ".json")
        saved = json.loads(p.read_text())
        assert saved["candidate"] == original
        assert saved["details"]["field_error"]
        for i, section in enumerate(parsed.sections):
            if i != index:
                assert section.reason == original["sections"][i]["reason"]
    finally:
        r.close()


def test_repair_cannot_change_facts_or_unrelated_sections():
    original = {"sections": [{"code":"N"}, {"code":"R"}], "facts": {"reason":"original"}}
    with pytest.raises(ValueError):
        merge_repair(original, {"sections":[{"code":"R"}], "facts_reason":""}, ["N"])
    with pytest.raises(ValueError):
        merge_repair(original, {"sections":[{"code":"N"}], "facts_reason":"changed"}, ["N"])
    with pytest.raises(ValueError):
        merge_repair(original, {"sections":[{"code":"N"}], "facts_reason":"", "facts":{}}, ["N"])


def test_sse_timeout_retains_first_character_id_and_original_text(tmp_path):
    s = Settings(_env_file=None, database_path=str(tmp_path / "nested/db.sqlite"))
    trace = PostStreamTrace(s)
    class Response:
        headers = {"content-type":"text/event-stream"}  # noqa: RUF012 - immutable test fixture
        def iter_lines(self):
            yield 'data: {"id":"test-id","choices":[{"delta":{"content":"原始片段"}}]}'
            raise httpx.ReadTimeout("test")
    from time import monotonic
    with pytest.raises(httpx.ReadTimeout):
        _read_chat_response(Response(), started=monotonic(), timeout=2, max_bytes=1000, progress=trace)
    state = trace.finish()
    assert state["model_first_byte_ms"] is not None
    assert state["model_complete_ms"] is None
    assert state["model_request_id"] == "test-id"
    assert json.loads(trace.path.read_text())["generated_text"] == "原始片段"


def test_wall_clock_timeout_preserves_live_progress(tmp_path, monkeypatch):
    r = reviewer(tmp_path, llm_evaluation_timeout_seconds=0.02, llm_format_retries=0,
                 llm_evaluation_unlimited_generation=False)
    done = Event()
    def request(messages, precheck, timeout, lease, progress=None, repair=False):
        try:
            progress.update(model_first_byte_ms=1, model_request_id="slow-id", phase="generating", text="部分输出")
            done.wait(0.08)
            return {}, {}
        finally:
            lease.release()
    monkeypatch.setattr(r,"_request",request)
    attempts = []
    try:
        with pytest.raises(ModelCallError, match="timeout"):
            r._analyze(visit(),False,attempts)
        assert attempts[0].model_first_byte_ms == 1
        assert attempts[0].model_request_id == "slow-id"
        assert attempts[0].timeout_phase == "generating"
        assert attempts[0].model_complete_ms is None
        assert attempts[0].stream_evidence_id
    finally:
        done.set()
        r.close()


def test_diagnostic_write_failure_is_visible(tmp_path, monkeypatch):
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("test")
    r = ChatModelReviewer(Settings(_env_file=None, database_path=str(blocker / "db.sqlite"),
        llm_format_retries=0), load_taoran_knowledge_snapshot())
    def request(messages, precheck, timeout, lease, **kwargs):
        lease.release()
        raise ModelCallError("invalid_json")
    monkeypatch.setattr(r, "_request", request)
    attempts = []
    try:
        with pytest.raises(ModelCallError):
            r._analyze(visit(), False, attempts)
        assert attempts[0].diagnostic_save_failed
        assert attempts[0].diagnostic_evidence_id is None
    finally:
        r.close()


def test_invalid_json_keeps_partial_raw_text_private(tmp_path):
    trace = PostStreamTrace(Settings(_env_file=None, database_path=str(tmp_path / "db.sqlite")))
    class Response:
        headers = {"content-type": "text/event-stream"}  # noqa: RUF012 - immutable test fixture
        def iter_lines(self):
            yield 'data: {"id":"raw-id","choices":[{"delta":{"content":"{坏JSON"}}]}'
            yield 'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}'
    from time import monotonic
    envelope, _, _ = _read_chat_response(Response(), started=monotonic(), timeout=2, max_bytes=1000, progress=trace)
    trace.finish()
    assert envelope["choices"][0]["message"]["content"] == "{坏JSON"
    assert json.loads(trace.path.read_text())["generated_text"] == "{坏JSON"


def test_queue_timeout_is_not_generation_timeout(tmp_path, monkeypatch):
    r = reviewer(tmp_path)
    monkeypatch.setattr(r.model_capacity, "acquire", lambda *args: None)
    attempts = []
    try:
        with pytest.raises(ModelCallError, match="queue_timeout"):
            r._analyze(visit(), False, attempts)
        assert attempts[0].timeout_phase == "local_queue"
        assert attempts[0].latency_ms == 0
        assert attempts[0].model_first_byte_ms is None
        assert attempts[0].diagnostic_evidence_id
    finally:
        r.close()
