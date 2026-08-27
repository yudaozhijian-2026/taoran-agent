from __future__ import annotations

from typing import Any

import httpx

from .config import Settings


class JiandaoyunReadError(RuntimeError):
    pass


def get_jiandaoyun_record(
    settings: Settings,
    tenant_id: str,
    app_id: str,
    entry_id: str,
    data_id: str,
) -> dict[str, Any]:
    api_key = settings.jiandaoyun_api_keys.get(tenant_id)
    if not api_key:
        raise JiandaoyunReadError("未配置当前租户的简道云API密钥")
    try:
        response = httpx.post(
            f"{settings.jiandaoyun_base_url.rstrip('/')}/v5/app/entry/data/get",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={"app_id": app_id, "entry_id": entry_id, "data_id": data_id},
            timeout=settings.jiandaoyun_timeout_seconds,
        )
        response.raise_for_status()
        body = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise JiandaoyunReadError("简道云记录读取失败") from exc
    record = body.get("data") if isinstance(body, dict) else None
    if not isinstance(record, dict) or str(record.get("_id", "")) != data_id:
        raise JiandaoyunReadError("简道云返回的记录与目标数据ID不匹配")
    return record
