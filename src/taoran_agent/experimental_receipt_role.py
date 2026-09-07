"""Experimental interpretation of a user-clarified receipt actor, not a score."""
import re


def proxy_receipt_goal_conflict(text: str, context: dict) -> bool:
    """Reject substituting customer acceptance for the clarified sales action."""
    return bool(receipt_role_hint(context) and re.search(
        r"不足以证明[^。；]{0,45}(?:由客户确认|客户[^。；]{0,10}(?:签收|接收))", text,
    ))


def receipt_role_hint(context: dict) -> str:
    if (context.get("expected_key_result") != "现场收货"
            or context.get("process_description") != "现场给客户收货，送卡"):
        return ""
    return (
        "\nexperimental第7条原文的用户业务澄清：‘现场给客户收货，送卡’中的收货主体是销售，"
        "含义是销售替客户收货，不是客户接收或签收。分析应明确销售代收这一动作。"
        "客户是否签收与销售代收是不同环节，不得因未记录客户签收就否定销售代收动作或"
        "自动判代收目标未达成。也不得据此宣称客户已验收、满意或关系拉近。"
        "此澄清仅解释主体，不代表业务已确认所有目标及自评均合格；原字段不改写，"
        "证据仍引用原字段连续原文，不能把本提示冒充客户原话。"
        "若代收细节不足，可针对代收对象或结果的记录清晰度说明，不强制增加客户签收作为代收前提。"
        "O_KR建议若指出目标不具体，只围绕销售代收的事项、范围或交接要求，不得改问客户由谁确认接收验收。"
        "R仍按既有客观记录标准校验，但建议必须承认销售代收动作已记录，"
        "区分未记录客户反馈和未发生代收；不得把缺少客户反馈说成销售代收目标未完成。"
        "分析中若提到自评，要说明不能用客户未签收反驳销售代收；不以‘自评达成但客户未接收’制造因果差距。"
    )
