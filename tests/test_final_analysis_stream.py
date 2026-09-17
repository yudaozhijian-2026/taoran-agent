from taoran_agent.api import _quick_check_final_analysis
from taoran_agent.front_v46.observed_feedback import _AnalysisPointStream


def test_analysis_point_stream_emits_partial_text_without_json_syntax():
    emitted = []
    stream = _AnalysisPointStream(emitted.append)
    chunks = [
        '{"analysis_',
        'points":[{"kind":"visit_context","text":"客户已确认',
        '设备\u6e05单。","proofs":[]},{"kind":"next_step","text":"待跟进电源准备","proofs":[]}],',
        '"items":[{"code":"N","suggestion":"补充时间"}]}',
    ]
    stream.feed(chunks[0])
    assert emitted == []
    stream.feed(chunks[1])
    assert emitted == ["客户已确认"]
    stream.feed(chunks[2])
    stream.feed(chunks[3])
    assert "".join(emitted) == "客户已确认设备清单。待跟进电源准备。"
    assert '"text"' not in "".join(emitted)


def test_analysis_point_stream_decodes_split_json_escape():
    emitted = []
    stream = _AnalysisPointStream(emitted.append)
    stream.feed('{"analysis_points":[{"text":"客户确\\u')
    stream.feed('8ba4清单")],"items":[]}')
    assert "".join(emitted) == "客户确认清单。"


def test_final_analysis_excludes_suggestions_and_confirmation_tail():
    text = """
本次拜访分析：
客户已确认设备清单。

智能填写建议：
1、补充时间。

需确认补充事项：
1、核对状态。
"""
    assert _quick_check_final_analysis(text) == "客户已确认设备清单。"
