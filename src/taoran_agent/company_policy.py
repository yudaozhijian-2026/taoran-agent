"""Company-wide TAORAN policies that tenants cannot override."""

from __future__ import annotations

from datetime import timedelta

COMPANY_POLICY_VERSION = "TAORAN-COMPANY-POLICY-20260921-V2"
TERMINAL_OPPORTUNITY_STAGE = "P6"
SUBMISSION_TIMELINESS_HOURS = 24
SUBMISSION_TIMELINESS_WINDOW = timedelta(hours=SUBMISSION_TIMELINESS_HOURS)


def public_company_policy() -> dict[str, object]:
    return {
        "policy_version": COMPANY_POLICY_VERSION,
        "submission_timeliness": {
            "scope": "company",
            "tenant_configurable": False,
            "window_hours": SUBMISSION_TIMELINESS_HOURS,
            "standard": (
                "面对面拜访在拜访结束基准后0—24小时内提交为及时；"
                "非面对面拜访在拜访日期当日或次日提交为及时。"
            ),
            "face_to_face": "拜访结束基准后0—24小时内。",
            "non_face_to_face": "拜访日期当日或次日（按中国时区自然日）。",
        },
        "opportunity_stage": {
            "terminal_stage": TERMINAL_OPPORTUNITY_STAGE,
            "purpose_matching_after_terminal": False,
        },
        "next_action_target": {
            "meaning": "current_customer",
            "specific_contact_required": False,
        },
    }
