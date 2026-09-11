"""Read-only, tenant-scoped CSV export for Excel (UTF-8 BOM)."""
from __future__ import annotations

import csv
import json
import sqlite3
from collections import defaultdict
from contextlib import closing
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")
STAGES = {"frontend_preview": "前端预览", "frontend_final": "前端Final", "backend": "后端检测"}


def _csv(path, rows, headers):
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: ("'" + value if isinstance(value, str)
                                   and value.startswith(("=", "+", "-", "@", "\t", "\r")) else value)
                             for key, value in row.items()})


def export_usage(database, output, tenant_id, start, end, directory=None):
    start, end = date.fromisoformat(start), date.fromisoformat(end)
    if end < start:
        raise ValueError("结束日期必须不早于开始日期")
    people = {}
    if directory:
        for person in json.loads(Path(directory).read_text(encoding="utf-8"))["sales"]:
            if person["tenant_id"] == tenant_id:
                if person["employee_id"] in people:
                    raise ValueError("销售名录中存在重复编号，请先核实")
                people[person["employee_id"]] = person
    lower = datetime.combine(start, time.min, SHANGHAI)
    upper = datetime.combine(end + timedelta(days=1), time.min, SHANGHAI)
    lower_utc, upper_utc = lower.astimezone(UTC).isoformat(), upper.astimezone(UTC).isoformat()
    uri = Path(database).resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as db:
        db.execute("PRAGMA query_only=ON")
        db.row_factory = sqlite3.Row
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                          "AND name='model_token_usage'").fetchone():
            raise ValueError("该数据库尚无Token记账表；需要部署后实际调用，不能把历史缺失当作零")
        rows = [dict(r) for r in db.execute(
            "SELECT * FROM model_token_usage WHERE tenant_id=? AND started_at>=? "
            "AND started_at<? ORDER BY started_at,call_id",
            (tenant_id, lower_utc, upper_utc),
        )]
        # Empty-tenant calls must be investigated, never silently attributed to a tenant.
        unassigned = [dict(r) for r in db.execute(
            "SELECT started_at FROM model_token_usage WHERE tenant_id='' "
            "AND started_at>=? AND started_at<?", (lower_utc, upper_utc)
        )]
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    details, excluded = [], []
    daily, personal = defaultdict(list), defaultdict(list)
    for row in rows:
        owner = row["employee_id"]
        person = people.get(owner, {})
        day = datetime.fromisoformat(row["started_at"]).astimezone(SHANGHAI).date().isoformat()
        item = {
            "调用日期（北京时间）": day,
            "调用时间（北京时间）": datetime.fromisoformat(row["started_at"]).astimezone(SHANGHAI).isoformat(),
            "销售姓名": person.get("name", "未核实姓名"), "所属公司": person.get("company", "未核实公司"),
            "销售编号": owner, "调用环节": STAGES.get(row["stage"], "其他/待核实"),
            "已核实Token": row["total_tokens"] if row["usage_status"] == "known" else None,
            "输入Token": row["input_tokens"], "输出Token": row["output_tokens"],
            "缓存输入Token（已包含在输入中）": row["cached_input_tokens"],
            "用量状态": {"known": "已核实", "unknown": "未知", "inconsistent": "服务商字段不一致"}[row["usage_status"]],
            "调用状态": row["call_status"], "记录ID": row["record_id"],
            "拜访记录编码": row["record_code"], "业务请求ID": row["request_id"],
            "调用ID": row["call_id"], "服务商请求ID": row["provider_request_id"], "模型": row["model"],
        }
        if person.get("exclude", False):
            excluded.append(item)
        else:
            details.append(item)
            daily[day, owner].append(row)
            personal[owner].append(row)

    def summary(group, owner, day):
        person = people.get(owner, {})
        known = [r for r in group if r["usage_status"] == "known"]
        unknown = len(group) - len(known)
        subtotal = sum(r["total_tokens"] for r in known)
        front = sum(r["total_tokens"] for r in known if r["stage"].startswith("frontend"))
        result = {"日期/期间": day, "销售姓名": person.get("name", "未核实姓名"),
                  "所属公司": person.get("company", "未核实公司"), "销售编号": owner,
                  "模型调用数（含重试）": len(group),
                  "前端预览已核实Token": sum(r["total_tokens"] for r in known if r["stage"] == "frontend_preview"),
                  "前端Final已核实Token": sum(r["total_tokens"] for r in known if r["stage"] == "frontend_final"),
                  "前端合计已核实Token": front,
                  "后端检测已核实Token": sum(r["total_tokens"] for r in known if r["stage"] == "backend"),
                  "其他环节已核实Token": sum(r["total_tokens"] for r in known if r["stage"] not in STAGES),
                  "已核实Token小计": subtotal, "用量未知/异常调用数": unknown,
                  "完整总Token": subtotal if not unknown else None,
                  "完整性": "已捕获调用用量完整" if not unknown else "不完整，不能将小计视为总量"}
        return result

    daily_rows = [summary(group, owner, day) for (day, owner), group in sorted(daily.items())]
    period = f"{start} 至 {end}"
    person_rows = [summary(group, owner, period) for owner, group in sorted(personal.items())]
    all_sales = [row for group in personal.values() for row in group]
    total = summary(all_sales, "", period)
    total.update({"销售姓名": "正式销售合计", "所属公司": "全部", "销售编号": ""})
    headers = list(total)
    _csv(output / "01_每人每天Token.csv", daily_rows, headers)
    _csv(output / "02_每人期间合计及总计.csv", [*person_rows, total], headers)
    detail_headers = list((details or excluded or [{
        k: None for k in ["调用日期（北京时间）", "销售姓名", "所属公司", "销售编号", "调用环节", "已核实Token", "用量状态"]
    }])[0])
    _csv(output / "03_正式销售调用明细.csv", details, detail_headers)
    _csv(output / "04_已排除测试人员调用明细.csv", excluded, detail_headers)
    unassigned_count = sum(lower <= datetime.fromisoformat(r["started_at"]) < upper for r in unassigned)
    note = (
        f"统计租户：{tenant_id}\n期间：{period}（北京时间，包含首尾日期）\n"
        "统计依据：拜访记录所属销售编号。姓名和任职公司来自提供的核实名录，名录需随人员变化维护。\n"
        "每次真实模型请求单独记账；重试也计入。缓存命中无新模型请求则不新增用量。\n"
        "前端包括预览、Final及其模型校验；后端包括提交后的评分及自动重试。\n"
        "已保存记录手动检查若触发评分，归后端检测；预览归前端预览。用量阶段不等同于主动点击次数。\n"
        "缓存输入已经包含在输入Token中，不能再加一次。\n"
        "完整总Token仅在已捕获调用均有可核实总量时填写；空白表示不完整。\n"
        "表中仅列出有捕获调用的人员和日期。未列出不等于历史零消耗。\n"
        "部署前缺失的历史用量无法由此恢复；服务中断、数据库记录失败也会造成缺口。\n"
        "程序日志TOKEN_USAGE_PERSIST_FAILED必须排查，否则即使表内完整也不能认定期间全量完整。\n"
        f"期间未归属租户的模型调用：{unassigned_count} 次（全服务待核实，未计入本租户合计）。\n"
        f"按名录排除测试调用：{len(excluded)} 次；其明细保留在04文件，不计入正式销售总量。\n"
    )
    (output / "统计口径.txt").write_text(note, encoding="utf-8")
    return {"sales": len(personal), "calls": len(details), "excluded_calls": len(excluded),
            "unattributed_calls": unassigned_count, "known_tokens": total["已核实Token小计"],
            "unknown_calls": total["用量未知/异常调用数"], "output": str(output.resolve())}
