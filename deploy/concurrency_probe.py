"""Run a synthetic concurrent button-check probe without Jiandaoyun writes."""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import statistics
from datetime import datetime
from pathlib import Path
from time import perf_counter

import httpx
from internal_probe import synthetic_visit

from taoran_agent.config import get_settings


def ai_error_reason(text: str) -> str | None:
    for pattern in (
        r"AI调用异常[。.]异常原因[：:]\s*([^。\n]+)",
        r"AI调用异常[：:]\s*([^。\n]+)",
    ):
        match = re.search(pattern, text)
        if match:
            return match.group(1).strip()
    return None


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    position = max(0, min(len(ordered) - 1, round((len(ordered) - 1) * fraction)))
    return ordered[position]


async def run_probe(base: str, users: int, ui_budget: float) -> dict:
    settings = get_settings()
    api_key = settings.tenant_keys["tenant_demo"]
    endpoint = base.rstrip("/") + "/api/v1/connectors/jiandaoyun/visit/button-check"
    gate = asyncio.Event()
    batch_started = perf_counter()

    limits = httpx.Limits(max_connections=users, max_keepalive_connections=users)
    timeout = httpx.Timeout(max(120.0, ui_budget * 4))
    headers = {"X-API-Key": api_key}

    async with httpx.AsyncClient(limits=limits, timeout=timeout, follow_redirects=False) as client:
        async def one(index: int) -> dict:
            payload = {
                "tenant_id": "tenant_demo",
                "user_id": f"concurrency-user-{index:02d}",
                "source_record_id": f"SYNTHETIC-CONCURRENCY-{index:02d}",
                **synthetic_visit(),
            }
            payload["employee_id"] = f"synthetic-{index:02d}"
            await gate.wait()
            started = perf_counter()
            try:
                response = await client.post(endpoint, json=payload, headers=headers)
                elapsed = perf_counter() - started
                item = {
                    "user": index,
                    "http": response.status_code,
                    "elapsed_seconds": round(elapsed, 3),
                    "within_ui_budget": elapsed <= ui_budget,
                }
                try:
                    body = response.json()
                except ValueError:
                    item["error"] = "non_json_response"
                    return item
                if response.status_code != 200:
                    item["error"] = str(body.get("detail", "http_error"))[:160]
                    return item
                rule = body.get("rule_feedback_text", "")
                knowledge = body.get("knowledge_feedback_text", "")
                item.update(
                    server_latency_ms=body.get("latency_ms"),
                    rule_nonempty=bool(rule.strip()),
                    knowledge_nonempty=bool(knowledge.strip()),
                    knowledge_ai_error="AI调用异常" in knowledge,
                    knowledge_error_reason=ai_error_reason(knowledge),
                    rule_status=body.get("rule_status"),
                    knowledge_status=body.get("knowledge_status"),
                    knowledge_reference_count=len(body.get("live_knowledge_references", [])),
                )
                return item
            except httpx.TimeoutException:
                return {
                    "user": index,
                    "http": None,
                    "elapsed_seconds": round(perf_counter() - started, 3),
                    "within_ui_budget": False,
                    "error": "transport_timeout",
                }
            except httpx.HTTPError as exc:
                return {
                    "user": index,
                    "http": None,
                    "elapsed_seconds": round(perf_counter() - started, 3),
                    "within_ui_budget": False,
                    "error": type(exc).__name__,
                }

        tasks = [asyncio.create_task(one(index)) for index in range(1, users + 1)]
        gate.set()
        results = await asyncio.gather(*tasks)

    elapsed_values = [float(item["elapsed_seconds"]) for item in results]
    successful = [item for item in results if item.get("http") == 200]
    clean_two = [
        item for item in successful
        if item.get("rule_nonempty")
        and item.get("knowledge_nonempty")
        and not item.get("knowledge_ai_error")
    ]
    return {
        "synthetic_only": True,
        "jiandaoyun_write": False,
        "base": base,
        "users": users,
        "configured_model": settings.llm_model,
        "configured_llm_max_concurrency": settings.llm_max_concurrency,
        "ui_budget_seconds": ui_budget,
        "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "batch_elapsed_seconds": round(perf_counter() - batch_started, 3),
        "summary": {
            "http_200": len(successful),
            "two_feedback_clean": len(clean_two),
            "within_ui_budget": sum(bool(item["within_ui_budget"]) for item in results),
            "knowledge_ai_errors": sum(bool(item.get("knowledge_ai_error")) for item in results),
            "transport_or_http_errors": sum(item.get("http") != 200 for item in results),
            "mean_seconds": round(statistics.mean(elapsed_values), 3),
            "p50_seconds": round(percentile(elapsed_values, 0.50), 3),
            "p95_seconds": round(percentile(elapsed_values, 0.95), 3),
            "max_seconds": round(max(elapsed_values, default=0.0), 3),
        },
        "results": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="https://taoran.yudaozhijian.top")
    parser.add_argument("--users", type=int, default=10)
    parser.add_argument("--ui-budget", type=float, default=15.0)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not 1 <= args.users <= 50:
        raise SystemExit("users must be between 1 and 50")
    result = asyncio.run(run_probe(args.base, args.users, args.ui_budget))
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()
