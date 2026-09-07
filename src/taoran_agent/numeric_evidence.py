"""Compare complete numeric values, permitting deterministic display equivalents."""
import re
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo


def numeric_tokens(text):
    # Match units as part of values: 3万 is not interchangeable with 3元.
    return {(Decimal(number.replace(",", "")) * {"万": 10000, "千": 1000, "亿": 100000000}.get(unit, 1)).normalize()
            for number, unit in re.findall(r"(\d+(?:,\d{3})*(?:\.\d+)?)([万千亿]?)", text)}


def chinese_number(match):
    digits = dict(zip("零一二三四五六七八九", range(10)))
    digits["两"] = 2
    word = match.group()
    if not any(c in word for c in "十百千万亿"):
        return "".join(str(digits[c]) for c in word)
    total = section = number = 0
    for char in word:
        if char in digits:
            number = digits[char]
        elif char in "十百千":
            section += (number or 1) * {"十": 10, "百": 100, "千": 1000}[char]
            number = 0
        else:
            section += number
            number = 0
            if char == "万":
                section *= 10000
            else:
                total = (total + section) * 100000000
                section = 0
    return str(total + section + number)


def missing_numeric_tokens(text, proofs, context=None):
    # A canonical stage identifier is background metadata, not a quantity in a
    # process quote. Check it against its own authoritative field. Never allow
    # an unrelated occurrence of the digit to prove a different stage.
    stage_pattern = r"(?<![A-Za-z0-9])P[1-6](?!\d)"
    unknown_stages = set()
    if context is not None:
        stage_sources = [str(context.get(key) or "") for key in ("opportunity_stage", "opportunity_stages")]
        stage_sources += [proof.quote for proof in proofs if proof.field in {"opportunity_stage", "opportunity_stages"}]
        known_stages = set(re.findall(stage_pattern, " ".join(stage_sources)))
        claimed_stages = set(re.findall(stage_pattern, text))
        unknown_stages = claimed_stages - known_stages
        text = re.sub(stage_pattern, "", text)
    evidence = []
    for proof in proofs:
        quote = proof.quote
        # Date-only strings keep their calendar day. Only aware timestamps can
        # be converted to the China calendar used by the frontend.
        if proof.field in {"next_contact_at", "visit_date"}:
            try:
                stamp = datetime.fromisoformat(quote)
                if stamp.tzinfo:
                    stamp = stamp.astimezone(ZoneInfo("Asia/Shanghai"))
                evidence.append(f"{stamp.year}年{stamp.month}月{stamp.day}日")
                continue
            except ValueError:
                pass
        evidence.append(quote)
        evidence.append(re.sub(r"[零一二三四五六七八九两十百千万亿]+", chinese_number, quote))
    return sorted(unknown_stages | {str(value) for value in numeric_tokens(text) - numeric_tokens(" ".join(evidence))})
