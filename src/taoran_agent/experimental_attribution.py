"""Bounded explicit-speaker checks; not a general coreference resolver."""
import re

_CUSTOMER = r"(?:客户|[\u4e00-\u9fff]{1,6}(?:部长|经理|主管|主任|负责人))"
_SALES = r"(?:(?:我方|我司)[\u4e00-\u9fff]{0,6}(?:经理|主管|负责人)|销售(?:人员|经理)?|我方|业务员|我)"
_VERB = r"(?:明确表示|反馈|表示|判断|认为|计划|说|称)"
_REPORT = re.compile(rf"(?P<speaker>{_SALES}|{_CUSTOMER})(?P<verb>{_VERB})(?P<body>[^。！？；;\n]+)")


def speaker_spans(source: str) -> list[dict[str, str]]:
    spans = []
    for match in _REPORT.finditer(source or ""):
        if re.search(r"(?:不是|并非|不要|不能|不应|如果|假如)$", source[max(0, match.start() - 3):match.start()]):
            continue
        speaker = match["speaker"]
        role = "sales" if re.fullmatch(_SALES, speaker) else "customer"
        body = match["body"]
        # A comma introducing another explicit actor ends this attribution.
        boundary = re.search(rf"[，,]\s*(?:(?:{_SALES}|{_CUSTOMER})(?:{_VERB}|介绍|继续|推动)|(?:给|向)客户介绍)", body)
        if boundary:
            body = body[:boundary.start()]
        # Nested reported speech is ambiguous; do not assert an owner here.
        if _REPORT.search(body):
            continue
        quote = speaker + match["verb"] + body
        spans.append({"role": role, "speaker": speaker, "quote": quote, "body": body})
    return spans


def attribution_hints(source: str) -> list[dict[str, str]]:
    return [{key: span[key] for key in ("role", "speaker", "quote")} for span in speaker_spans(source)][:12]


def _compact(text: str) -> str:
    return re.sub(r"[\s，,。；;：:“”\"'！？]", "", text)


def attribution_conflict(text: str, source: str) -> bool:
    """Reject reattribution of distinctive shared fragments (>=5 characters).

    Correct neutral summaries are allowed. Same wording explicitly used by both
    sides is not enough to infer a conflict. No replacement feedback is created.
    """
    original = speaker_spans(source)
    for output in speaker_spans(text):
        own = [_compact(item["body"]) for item in original if item["role"] == output["role"]]
        other = [_compact(item["body"]) for item in original if item["role"] != output["role"]]
        body = _compact(output["body"])
        for opposite in other:
            for index in range(max(0, len(opposite) - 4)):
                fragment = opposite[index:index + 5]
                if fragment in body and not any(fragment in value for value in own):
                    return True
    return False
