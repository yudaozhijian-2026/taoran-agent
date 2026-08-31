from __future__ import annotations

import argparse
import json
from pathlib import Path
from tempfile import NamedTemporaryFile

import uvicorn

from .calibration import CalibrationDataset, CalibrationThresholds, run_calibration
from .config import get_settings
from .knowledge import KnowledgeApiClient, render_snapshot_markdown, write_snapshot
from .mapping_sync import fetch_jiandaoyun_form_schema, synchronize_mapping
from .models import PostEvaluationRequest, PrecheckRequest, VisitDraftInput
from .runtime import build_agent


def _synthetic_visit(index: int) -> VisitDraftInput:
    data = {
        "employee_id": "synthetic", "visit_date": "2026-08-25",
        "customer_id": "SYNTHETIC-CUSTOMER",
        "customer_type_ii": "opportunity", "opportunity_stage": "P3",
        "visit_method": "face_to_face", "is_appointment": True,
        "purpose_code": "确认验证计划", "expected_key_result": "客户确认验证日期和三台设备清单",
        "process_description": (
            "技术负责人确认8月28日验证并确认三台设备清单。"
            "双方约定8月27日发送验证方案，客户书面确认验证范围。"
        ),
        "self_assessment": "achieved", "next_action_purpose": "发送验证方案并确认验证范围",
        "next_action_expected_result": "客户书面确认验证范围", "next_action_target_id": "synthetic",
        "next_contact_at": "2026-08-27T10:00:00+08:00",
    }
    if index % 3 == 1:
        data.update(
            process_description="技术负责人确认8月28日验证，但尚未确认设备清单。",
            self_assessment="partially_achieved",
            next_action_purpose="与技术负责人确认三台设备清单",
            next_action_expected_result="客户书面确认三台设备清单",
        )
    elif index % 3 == 2:
        data.update(
            expected_key_result="", process_description="", self_assessment="not_achieved",
            next_action_purpose="", next_action_expected_result="", next_contact_at=None,
        )
    return VisitDraftInput(**data)


def main() -> None:
    parser = argparse.ArgumentParser(description="DSM TAORAN拜访智能体")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("model-status", help="只检查大模型配置状态，不显示密钥或调用模型")
    model_parser = subparsers.add_parser(
        "test-model", help="测试前端本地规则与提交后模型，只用虚构样例，不读写简道云",
    )
    model_parser.add_argument("--samples", type=int, choices=range(1, 7), default=3)
    model_parser.add_argument(
        "--scenario", choices=["all", "complete", "partial", "missing"], default="all",
    )

    precheck_parser = subparsers.add_parser("precheck", help="执行提交前质检")
    precheck_parser.add_argument("input", type=Path)

    evaluation_parser = subparsers.add_parser("evaluate", help="执行提交后有效性评价")
    evaluation_parser.add_argument("input", type=Path)

    serve_parser = subparsers.add_parser("serve", help="启动HTTP服务")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8030)

    sync_parser = subparsers.add_parser(
        "sync-jiandaoyun-fields",
        help="从简道云V5字段接口自动同步测试副本widget ID",
    )
    sync_parser.add_argument("--tenant-id", required=True)
    sync_parser.add_argument(
        "--mapping",
        type=Path,
        help="字段映射文件；省略时使用该租户注册表中的mapping_path",
    )
    sync_parser.add_argument("--apply", action="store_true", help="确认后写回映射文件")

    calibration_parser = subparsers.add_parser(
        "calibrate",
        help="运行专家标注样例校准并生成差异报告",
    )
    calibration_parser.add_argument("input", type=Path)
    calibration_parser.add_argument("--output", type=Path)
    calibration_parser.add_argument("--q33-tolerance", type=float, default=0.005)
    calibration_parser.add_argument("--q34-tolerance", type=float, default=5.0)

    knowledge_parser = subparsers.add_parser(
        "sync-taoran-knowledge",
        help="从DSM知识服务同步已批准/已确认的TAORAN知识快照",
    )
    knowledge_parser.add_argument(
        "--output",
        type=Path,
        default=Path("src/taoran_agent/data/taoran_knowledge_snapshot_v1.json"),
    )
    knowledge_parser.add_argument(
        "--markdown",
        type=Path,
        default=Path("TAORAN知识库基线.md"),
    )
    knowledge_parser.add_argument("--apply", action="store_true", help="审核后写入快照和Markdown")

    args = parser.parse_args()
    if args.command in {"model-status", "test-model"}:
        settings = get_settings()
        if args.command == "model-status":
            print(json.dumps({
                "enabled": settings.llm_enabled,
                "model": settings.llm_model,
                "endpoint_configured": bool(settings.llm_api_url),
                "key_configured": bool(settings.llm_api_key),
                "precheck_mode": "local_rules",
                "precheck_model_calls": 0,
                "evaluation_timeout_seconds": settings.llm_evaluation_timeout_seconds,
                "format_retries": settings.llm_format_retries,
                "q40_config_used": False,
            }, ensure_ascii=False, indent=2))
            return
        if not settings.llm_enabled:
            parser.error("模型尚未启用，请先配置TAORAN专用模型接口、模型名称和Key")
        agent = build_agent(settings)
        passed = 0
        try:
            for index in range(args.samples):
                scenario_index = (
                    index % 3 if args.scenario == "all"
                    else ["complete", "partial", "missing"].index(args.scenario)
                )
                visit = _synthetic_visit(scenario_index)
                before = agent.precheck(PrecheckRequest(
                    context={"tenant_id": "synthetic", "user_id": "synthetic",
                             "request_id": f"probe-{index}"},
                    visit=visit,
                ))
                after = agent.semantic_reviewer.review_q34(visit)
                passed += int(
                    before.semantic_review.provider == "heuristic-v1"
                    and before.semantic_review.status == "completed"
                    and after.status == "completed"
                )
                print(json.dumps({
                    "sample": index + 1,
                    "scenario": ["complete", "partial", "missing"][scenario_index],
                    "synthetic_data_only": True, "jiandaoyun_writeback": False,
                    "precheck_mode": "local_rules", "precheck_model_calls": 0,
                    "precheck_status": before.status, "precheck_latency_ms": before.latency_ms,
                    "evaluation_status": after.status, "evaluation_latency_ms": after.latency_ms,
                    "evaluation_failure_reason": after.failure_reason,
                    "model": settings.llm_model, "prompt_version": after.prompt_version,
                    "model_attempts": [a.model_dump() for a in after.model_attempts],
                }, ensure_ascii=False, indent=2), flush=True)
        finally:
            close = getattr(agent.semantic_reviewer, "close", None)
            if close:
                close()
        print(json.dumps({"samples": args.samples, "passed": passed}, ensure_ascii=False))
        if passed != args.samples:
            raise SystemExit(1)
        return
    if args.command == "serve":
        uvicorn.run("taoran_agent.api:app", host=args.host, port=args.port)
        return
    if args.command == "sync-jiandaoyun-fields":
        settings = get_settings()
        configured_mapping_path = settings.jiandaoyun_mapping_path_for(args.tenant_id)
        if (
            not args.mapping
            and settings.tenant_config(args.tenant_id) is not None
            and not configured_mapping_path
        ):
            parser.error("当前租户尚未配置独立mapping_path")
        mapping_path = args.mapping or Path(
            configured_mapping_path or "config/jiandaoyun_field_mapping.example.json"
        )
        mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
        app_id = mapping.get("source_application_id")
        entry_id = mapping.get("source_entry_id")
        if not app_id or not entry_id:
            parser.error("映射文件缺少source_application_id或source_entry_id")
        schema = fetch_jiandaoyun_form_schema(
            settings, args.tenant_id, app_id, entry_id
        )
        updated, report = synchronize_mapping(mapping, schema)
        if args.apply:
            with NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=mapping_path.parent,
                prefix=f".{mapping_path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temp_file:
                temp_file.write(json.dumps(updated, ensure_ascii=False, indent=2) + "\n")
                temporary_path = Path(temp_file.name)
            temporary_path.replace(mapping_path)
        print(json.dumps({
            "applied": args.apply,
            "tenant_id": args.tenant_id,
            "mapping_path": str(mapping_path),
            **report,
        }, ensure_ascii=False, indent=2))
        return
    if args.command == "calibrate":
        dataset = CalibrationDataset.model_validate_json(args.input.read_text(encoding="utf-8"))
        report = run_calibration(
            dataset,
            CalibrationThresholds(
                q33_tolerance=args.q33_tolerance,
                q34_tolerance=args.q34_tolerance,
            ),
        )
        report_json = report.model_dump_json(indent=2)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(report_json + "\n", encoding="utf-8")
        print(report_json)
        return
    if args.command == "sync-taoran-knowledge":
        settings = get_settings()
        if not settings.knowledge_api_key:
            parser.error("请通过DSM_TAORAN_KNOWLEDGE_API_KEY配置应用专用Key")
        snapshot = KnowledgeApiClient(
            settings.knowledge_api_base_url,
            settings.knowledge_api_key.get_secret_value(),
            settings.knowledge_timeout_seconds,
        ).fetch_taoran_snapshot()
        if args.apply:
            write_snapshot(snapshot, args.output)
            args.markdown.write_text(render_snapshot_markdown(snapshot), encoding="utf-8")
        print(
            json.dumps(
                {
                    "applied": args.apply,
                    "record_count": snapshot.record_count,
                    "knowledge_ids": [record.id for record in snapshot.records],
                    "snapshot_hash": snapshot.snapshot_hash,
                    "output": str(args.output),
                    "markdown": str(args.markdown),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    payload = json.loads(args.input.read_text(encoding="utf-8"))
    agent = build_agent(get_settings())
    if args.command == "precheck":
        result = agent.precheck(PrecheckRequest.model_validate(payload))
    else:
        result = agent.evaluate(PostEvaluationRequest.model_validate(payload), "job_cli")
    print(result.model_dump_json(indent=2))
