from copy import deepcopy

import pytest
from pydantic import ValidationError
from test_agent import complete_precheck_payload

from taoran_agent import TaoranAgent
from taoran_agent.calibration import (
    CalibrationDataset,
    CalibrationThresholds,
    run_calibration,
)
from taoran_agent.models import PostEvaluationRequest


def evaluation_payload(request_id: str = "calibration-request") -> dict:
    payload = complete_precheck_payload(request_id)
    payload["visit"].update(
        {
            "actual_start_at": "2026-08-18T09:00:00+08:00",
            "actual_end_at": "2026-08-18T10:00:00+08:00",
            "submitted_at": "2026-08-18T11:00:00+08:00",
        }
    )
    return {
        "context": payload["context"],
        "visit_record_code": "CAL-001",
        "visit": payload["visit"],
        "opportunity_updated": True,
    }


def matching_dataset() -> CalibrationDataset:
    request_payload = evaluation_payload()
    response = TaoranAgent().evaluate(
        PostEvaluationRequest.model_validate(request_payload), "baseline"
    )
    return CalibrationDataset.model_validate(
        {
            "dataset_id": "dataset-test",
            "rule_version": "TAORAN-Q33-Q34-100-V2",
            "samples": [
                {
                    "sample_id": "sample-high-001",
                    "quality_band": "high",
                    "evaluation_request": request_payload,
                    "expert": {
                        "q33_score": response.q33_score,
                        "q34_score": response.q34_score,
                        "recommendation": response.count_as_effective_visit_recommendation,
                    },
                }
            ],
        }
    )


def test_calibration_computes_agreement_but_requires_full_dataset() -> None:
    report = run_calibration(matching_dataset())

    assert report.sample_count == 1
    assert report.q33_agreement_rate == 1
    assert report.q34_agreement_rate == 1
    assert report.serious_miss_count == 0
    assert report.ready_for_business_signoff is False
    assert report.sample_count_gate_passed is False
    assert report.band_balance_gate_passed is False


def test_calibration_can_pass_custom_small_test_thresholds() -> None:
    report = run_calibration(
        matching_dataset(),
        CalibrationThresholds(minimum_sample_count=1, minimum_samples_per_band=1),
    )

    assert report.sample_count_gate_passed is True
    assert report.band_balance_gate_passed is False


def test_calibration_detects_missed_critical_issue() -> None:
    payload = matching_dataset().model_dump(mode="json")
    payload["samples"][0]["expert"]["critical_issue_codes"] = ["EXPERT_CRITICAL_MISS"]

    report = run_calibration(CalibrationDataset.model_validate(payload))

    assert report.serious_miss_count == 1
    assert report.serious_miss_gate_passed is False
    assert report.results[0].missed_critical_issue_codes == ["EXPERT_CRITICAL_MISS"]


def test_calibration_rejects_writeback_targets() -> None:
    payload = matching_dataset().model_dump(mode="json")
    sample = deepcopy(payload["samples"][0])
    sample["evaluation_request"]["writeback_target"] = {
        "app_id": "app",
        "entry_id": "entry",
        "data_id": "data",
    }
    payload["samples"] = [sample]

    with pytest.raises(ValidationError, match="校准样例不得配置简道云回写目标"):
        CalibrationDataset.model_validate(payload)
