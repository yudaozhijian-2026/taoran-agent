# DSM TAORAN原则评分智能体

独立运行的TAORAN拜访评分Agent，版本`0.7.0`。已整合DSM知识库TAORAN标准，以及销售管理智能评测40题的Q33和Q34：

- Q33记录完整性与及时性：50分；
- Q34自评一致性与下一行动：50分；
- 综合满分：100分；
- 提交前AI检测只给修改建议，永不阻断提交；
- 提交前按`T—A—O/KR—R—A—N`六项本地知识规则快速检查，固定不调用大模型；
- 提交后由独立配置的智谱`glm-5.2`异步深度分析，规则计分后可通过API回写简道云。
- “AI检测”按钮只返回填写规范和修改建议，不生成、不展示、不回写正式评分。
- 正式评分仅在记录提交成功后触发，并回写“AI评分”和“AI反馈意见”。
- AI反馈不显示“知识依据”和“分析方式”两行；知识来源与模型信息仍保留在结构化结果和后台审计中。
- 为未来40题调用预留默认关闭的周期事实和批量补评接口，不修改或主动影响现有40题项目。

核心规则见[AGENT.md](AGENT.md)和[规则与评分.md](规则与评分.md)。接入步骤见[简道云接入.md](简道云接入.md)。
知识库原文基线和本地规则关系见[TAORAN知识库基线.md](TAORAN知识库基线.md)。
分阶段安排见[项目实施方案.md](项目实施方案.md)，未来Q40预留契约见[Q40调用预留接口.md](Q40调用预留接口.md)。
当前本地POC地址、运行条件和验证记录见[本地POC部署.md](本地POC部署.md)。
阶段5的专家样例标注和差异分析方法见[业务样例校准.md](业务样例校准.md)。
直接大模型接入、独立Key配置、超时保护及验收步骤见[大模型接入方案.md](大模型接入方案.md)。
大模型默认关闭；配置完成且真实联通验收通过后再启用，不把本地规则结果冒充模型分析。
0.6.0采用`TAORAN-LLM-FACTS-V2.1`格式契约，提交后格式不合规最多重生成一次，两次合计仍限45秒；空字段不生成空证据，证据不实、越权分数字段或调用失败均不能回写。真实测试记录见[智谱GLM-5.2接入验收记录.md](智谱GLM-5.2接入验收记录.md)。

## 快速运行

需要Python 3.12和`uv`：

```bash
uv sync --dev
uv run taoran-agent precheck examples/precheck_request.json
uv run taoran-agent evaluate examples/post_evaluation_request.json
uv run taoran-agent calibrate examples/calibration_dataset.example.json
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

## TAORAN知识同步

运行时使用项目内已审核的知识快照，不在按钮点击时访问远程知识库，避免网络延迟影响前端响应。需要更新知识时，由管理员在服务器环境中临时配置独立Key：

```text
DSM_TAORAN_KNOWLEDGE_API_KEY=replace-with-dedicated-api-key
```

先预览远端差异，确认后再更新本地JSON快照和根目录知识基线文档：

```bash
uv run taoran-agent sync-taoran-knowledge
uv run taoran-agent sync-taoran-knowledge --apply
```

完整Key只放在环境变量中，不写入项目文件或日志。

## 验证

```bash
uv run ruff check .
uv run pytest
uv build
```

## Docker

服务器独立部署配置见[服务器独立部署.md](服务器独立部署.md)及`deploy/compose.server.yaml`。部署目录已确认是`/TAORAN agent`，不得覆盖旧`/opt/taoran-agent`。独立HTTPS入口为`https://taoran.yudaozhijian.top`，公网测试和简道云待办见[HTTPS接入验收_20260826.md](HTTPS接入验收_20260826.md)；不要将接口就绪等同于简道云页面已经切换。
生产镜像使用固定Python3.12基础镜像和uv.lock；非root、单实例运行。不要将`.env`、历史数据或本地虚拟环境打入镜像。

```bash
docker build -t dsm-taoran-agent .
docker run --rm -p 127.0.0.1:8030:8030 -v dsm-taoran-data:/data dsm-taoran-agent
```

上述最小示例不含认证或模型配置，不能直接作为对外服务。实际受控试运行使用独立Compose配置和只读运行配置挂载。生产多实例部署需要把SQLite和进程内并发锁替换为PostgreSQL、共享幂等存储和可恢复任务队列。
