from __future__ import annotations

import argparse
import json
from pathlib import Path
from tempfile import NamedTemporaryFile

import uvicorn

from .agent import TaoranAgent
from .config import get_settings
from .mapping_sync import fetch_jiandaoyun_form_schema, synchronize_mapping
from .models import PostEvaluationRequest, PrecheckRequest


def main() -> None:
    parser = argparse.ArgumentParser(description="DSM TAORAN拜访智能体")
    subparsers = parser.add_subparsers(dest="command", required=True)

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
        default=Path("config/jiandaoyun_field_mapping.example.json"),
    )
    sync_parser.add_argument("--apply", action="store_true", help="确认后写回映射文件")

    args = parser.parse_args()
    if args.command == "serve":
        uvicorn.run("taoran_agent.api:app", host=args.host, port=args.port)
        return
    if args.command == "sync-jiandaoyun-fields":
        mapping = json.loads(args.mapping.read_text(encoding="utf-8"))
        app_id = mapping.get("source_application_id")
        entry_id = mapping.get("source_entry_id")
        if not app_id or not entry_id:
            parser.error("映射文件缺少source_application_id或source_entry_id")
        schema = fetch_jiandaoyun_form_schema(
            get_settings(), args.tenant_id, app_id, entry_id
        )
        updated, report = synchronize_mapping(mapping, schema)
        if args.apply:
            with NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=args.mapping.parent,
                prefix=f".{args.mapping.name}.",
                suffix=".tmp",
                delete=False,
            ) as temp_file:
                temp_file.write(json.dumps(updated, ensure_ascii=False, indent=2) + "\n")
                temporary_path = Path(temp_file.name)
            temporary_path.replace(args.mapping)
        print(json.dumps({"applied": args.apply, **report}, ensure_ascii=False, indent=2))
        return
    payload = json.loads(args.input.read_text(encoding="utf-8"))
    agent = TaoranAgent()
    if args.command == "precheck":
        result = agent.precheck(PrecheckRequest.model_validate(payload))
    else:
        result = agent.evaluate(PostEvaluationRequest.model_validate(payload), "job_cli")
    print(result.model_dump_json(indent=2))
