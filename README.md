# DSM TAORAN原则评分智能体

独立运行的TAORAN拜访评分Agent，版本`0.3.0`。已整合销售管理智能评测40题的Q33和Q34：

- Q33记录完整性与及时性：100分；
- Q34自评一致性与下一行动：100分；
- 综合满分：200分；
- 提交前AI检测只给修改建议，永不阻断提交；
- 提交后异步深度评价，并可通过API回写简道云。
- 为未来40题调用预留默认关闭的周期事实和批量补评接口，不修改或主动影响现有40题项目。

核心规则见[AGENT.md](AGENT.md)和[规则与评分.md](规则与评分.md)。接入步骤见[简道云接入.md](简道云接入.md)。
分阶段安排见[项目实施方案.md](项目实施方案.md)，未来Q40预留契约见[Q40调用预留接口.md](Q40调用预留接口.md)。
当前本地POC地址、运行条件和验证记录见[本地POC部署.md](本地POC部署.md)。

## 快速运行

需要Python 3.12和`uv`：

```bash
uv sync --dev
uv run taoran-agent precheck examples/precheck_request.json
uv run taoran-agent evaluate examples/post_evaluation_request.json
uv run taoran-agent serve --host 127.0.0.1 --port 8030
```

启动后访问`http://127.0.0.1:8030/docs`。

## 主要接口

- `POST /api/v1/connectors/jiandaoyun/visit/button-check`
- `POST /api/v1/connectors/jiandaoyun/visit/evaluations`
- `GET /api/v1/visit/evaluations/{job_id}`
- `POST /api/v1/visit/evaluations/{job_id}/writeback`

## 简道云回写配置

在`.env`中按租户配置API Key：

```text
DSM_TAORAN_JIANDAOYUN_API_KEYS_JSON={"tenant_demo":"replace-with-api-key"}
```

当前测试对象是简道云副本`拜访记录录入_AI测评`。应用ID、表单ID和29个输入/关联/输出映射项已通过
简道云V5接口核验并写入配置，未解析项为0；旧正式表字段ID不再作为活动配置。副本字段发生变化时可重新同步：

```bash
uv run taoran-agent sync-jiandaoyun-fields --tenant-id tenant_demo
uv run taoran-agent sync-jiandaoyun-fields --tenant-id tenant_demo --apply
```

第一条只预览，第二条才写回配置。请求样例见`examples/jiandaoyun_post_evaluation_request.json`。

## 验证

```bash
uv run ruff check .
uv run pytest
uv build
```

## Docker

```bash
docker build -t dsm-taoran-agent .
docker run --rm -p 8030:8030 -v dsm-taoran-data:/data dsm-taoran-agent
```

生产多实例部署需要把SQLite和进程内并发锁替换为PostgreSQL、共享幂等存储和可恢复任务队列。
