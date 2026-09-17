"""Versioned score units; legacy results remain readable, never silently rescaled."""

Q33_RULE_VERSION = "TAORAN-Q33-50-V2"
Q34_RULE_VERSION = "TAORAN-Q34-50-V2"
TOTAL_RULE_VERSION = "TAORAN-Q33-Q34-100-V2"
LEGACY_TOTAL_RULE_VERSION = "TAORAN-Q33-Q34-200-V1"
QUESTION_MAX_SCORE = 50
TOTAL_MAX_SCORE = 100
COMPLETENESS_MAX_SCORE = 25
TIMELINESS_MAX_SCORE = 25
CONSISTENCY_MAX_SCORE = 35
NEXT_ACTION_MAX_SCORE = 15

# Isolated TAORAN test policy: score each submitted visit against the fixed
# standards below, then map the resulting compliance rate to the 4/3/2/1/0
# band.  This does not change the dormant Q40 period-integration contract.
Q34_WEIGHTED_POLICY_VERSION = "TAORAN-Q34-WEIGHTED-TEST-V1"
Q34_WEIGHTED_THRESHOLDS = (0.95, 0.85, 0.75, 0.60)
Q34_SELF_EVALUATION_WEIGHTS = {
    "appointment_standard": 0.05,
    "key_result_quality_and_purpose_alignment": 0.25,
    "process_customer_facts": 0.20,
    "achievement_evidence_support": 0.15,
    "system_self_assessment_consistency": 0.35,
}
Q34_NEXT_ACTION_WEIGHTS = {
    "purpose_present": 0.20,
    "expected_result_present": 0.20,
    "contact_date_valid": 0.15,
    "customer_type_standard": 0.25,
    "semantic_logic_and_continuity": 0.20,
}
