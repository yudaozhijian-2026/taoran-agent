# DSM TAORAN原则评分智能体

独立运行的TAORAN拜访评分Agent，版本`0.13.1`。已整合DSM知识库TAORAN标准，以及销售管理智能评测40题的Q33和Q34：

- Q33记录完整性与及时性：50分；
- Q34自评一致性与下一行动：50分；
- 综合满分：100分；
- 提交前AI检测只给修改建议，永不阻断提交；
- 保留一个“AI检测”按钮，每次点击同时返回规则、实时知识库、纯大模型三份反馈；
- 规则反馈保持原`T—A—O/KR—R—A—N`六项本地检查逻辑不变；
- 知识库反馈每次实时调用DSM知识库API，只有成功取得内容后才交给智谱模型，不回退到打包快照；
- 大模型反馈只向智谱模型提供当前拜访内容和六项分析任务，不注入知识库内容；
- 提交后重新生成知识库反馈和纯大模型反馈并回写各自字段；深度评分证据校验失败时，这两个非评分反馈仍独立保留；
- 提交后由独立配置的智谱`glm-5.2`异步深度分析，规则计分后可通过API回写简道云。
- “AI检测”按钮只返回填写规范和修改建议，不生成、不展示、不回写正式评分。
- 正式评分仅在记录提交成功后触发，并回写“AI评分”和“AI反馈意见”。
- AI反馈不显示“知识依据”和“分析方式”两行；知识来源与模型信息仍保留在结构化结果和后台审计中。
- 为未来40题调用预留默认关闭的周期事实和批量补评接口，不修改或主动影响现有40题项目。
- 支持版本化多租户注册表；不同客户分别使用访问Key、简道云API Key、Webhook Secret和字段映射，旧单租户环境变量继续兼容。
- 同一个简道云应用/表单只能接入一个客户；已接入表单不可重复选择，历史重复记录在客户列表中归并显示。
- 客户配置结果和客户列表均提供后续部署指引，集中显示字段确认、插件、数据推送和真实测试步骤及可复制接口。
- 部署指引为可展开的逐步操作教程，包含完整插件字段绑定表、验收标准、常见问题和不含密钥的交付清单。
- 提供独立的部署完成/运行状态视图，以真实AI检测、提交后评价和回写记录判断各客户上线状态。

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

## 提交前三份反馈

`POST /api/v1/connectors/jiandaoyun/visit/button-check`在一次请求中返回：

- `rule_feedback_text` → AI反馈意见（规则反馈）；
- `knowledge_feedback_text` → AI反馈意见（知识库反馈）；
- `model_feedback_text` → AI反馈意见（大模型反馈）。

`feedback_text`继续等于`rule_feedback_text`，用于兼容旧简道云输出映射。三份反馈都不阻断提交、不生成提交前正式分数。知识库或模型分支失败时，只在对应字段返回“未完成”，不会用规则或本地快照冒充结论。

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

当前测试对象是简道云副本`拜访记录录入_AI测评`。应用ID、表单ID和31个输入/关联/输出映射项已通过
简道云V5接口核验并写入配置，未解析项为0；旧正式表字段ID不再作为活动配置。副本字段发生变化时可重新同步：

```bash
uv run taoran-agent sync-jiandaoyun-fields --tenant-id tenant_demo
uv run taoran-agent sync-jiandaoyun-fields --tenant-id tenant_demo --apply
```

第一条只预览，第二条才写回配置。请求样例见`examples/jiandaoyun_post_evaluation_request.json`。

## TAORAN知识同步

默认规则引擎仍使用项目内已审核快照；“知识库反馈”则每次按钮点击实时访问远程知识库API。服务器必须通过环境变量配置独立Key：

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

## 多客户配置

新客户使用`config/tenant_registry.example.json`建立服务器受控租户注册表。注册表支持租户停用、最多两个访问Key的
无停机轮换，以及每租户独立的简道云API Key、Webhook Secret和字段映射。详细目录、迁移和验收步骤见
[多租户配置与客户接入.md](多租户配置与客户接入.md)。现有`tenant_demo`在未启用注册表时继续使用旧环境变量，不改变当前流程。

商业化接入可启用中文管理页`/admin/tenants`：先用客户独立API Key自动读取已授权应用和表单，
再选择目标表单；客户编号、字段匹配、接入密钥、注册表写入和配置重载均自动完成。该功能默认关闭，
详见[客户接入管理页.md](客户接入管理页.md)。
未自动匹配的字段会显示中文明细和同范围候选字段，可手动映射或在简道云修改后重新检查；全部确认后自动启用客户。
表单选错时可返回重新选择，更新原客户配置且不轮换客户编号或现有接入密钥。
