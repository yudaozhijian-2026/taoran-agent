> 文档入口：[文档导航](文档导航.md)（线上版本、当前未部署开发、历史报告分类）。

# DSM TAORAN原则评分智能体

独立运行的TAORAN拜访评分Agent，当前主源码基线为`0.27.3rc2`（提交后解释政策V4.4）。已整合DSM知识库TAORAN标准，以及销售管理智能评测40题的Q33和Q34：

- Q33记录完整性与及时性：50分；
- Q34自评一致性与下一行动：50分；
- 综合满分：100分；
- 提交前AI检测只给修改建议，永不阻断提交；
- 保留一个“AI检测”按钮，每次点击同时返回规则、实时知识库、纯大模型三份反馈；
- 规则反馈保持`T—A—O/KR—R—A—N`六项结构；T-03按知识ID`DSM-BS-01-06`精确抓取并结构化客户类型/商机P1—P5目的映射，保留P5“协助项目实施”，强制排除P6“争取客户满意”；
- 知识库反馈每次实时调用DSM知识库API，只有成功取得内容后才交给智谱模型，不回退到打包快照；
- 规则反馈只使用本地规则和已发布知识快照，不调用知识库API或大模型；
- 知识库反馈实时检索知识库，并结合拜访数据调用智谱模型生成；
- 提交后重新生成知识库反馈并回写；深度评分证据校验失败时，该非评分反馈仍独立保留；
- 提交后由独立配置的智谱`glm-5.2`异步深度分析，规则计分后可通过API回写简道云。
- “AI检测”按钮只返回填写规范和修改建议，不生成、不展示、不回写正式评分。
- 正式评分仅在记录提交成功后触发，并回写“AI评分”和“AI反馈意见”。
- AI反馈不显示“知识依据”和“分析方式”两行；知识来源与模型信息仍保留在结构化结果和后台审计中。
- 为未来40题调用预留默认关闭的周期事实和批量补评接口，不修改或主动影响现有40题项目。
- 支持版本化多租户注册表；不同客户分别使用访问Key、简道云API Key、Webhook Secret和字段映射，旧单租户环境变量继续兼容。

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

## 提交前两份反馈

`POST /api/v1/connectors/jiandaoyun/visit/button-check`在一次请求中返回：

- `rule_feedback_text` → AI反馈意见（规则反馈）；
- `knowledge_feedback_text` → AI反馈意见（知识库反馈）。

`feedback_text`继续等于`rule_feedback_text`，用于兼容旧简道云输出映射。两份反馈都不阻断提交、不生成提交前正式分数。同一接口内两条任务并行执行；知识库或模型分支失败时，只在知识库字段返回具体AI调用异常，不影响规则反馈。

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

0.12.3已按2026-08-28正式确认的N-01至N-06公司标准统一下一步客户行动检查：对象默认为当前客户，日期按北京时间自然日及客户类型自然周期判断，目的和期望结果须承接本次客户事实，商机客户须有可核验的下一步客户共识。该口径同时进入三份提交前反馈和提交后Q34；前端等待30秒、模型分支等待18秒，以适配三反馈实际耗时。

0.12.4修正T-03生产知识结构化边界：按`DSM-BS-01-06`当前正式正文保留P5“协助项目实施”，只过滤P6“争取客户满意”；同时兼容“按P1-P6使用通用目的和阶段目的”的知识写法，防止有效知识被误报为映射无效。

0.12.6为三反馈按钮增加有界先来先服务队列和单次检测成对调度：并发4对应同时处理2次按钮检测，每次绑定纯大模型与知识库模型两个分支；最多8次点击等待12秒。队列满或等待超时时，规则反馈仍正常返回，两个模型反馈统一显示具体繁忙原因，不再因抢占顺序出现同一用户只完成一份模型反馈。模型底层槽位也改为在单次调用预算内有限等待，不再抢不到名额立即返回`busy`。

0.12.7针对已取得处理名额后`glm-5.2`偶发超过18秒的问题，将提交前每个模型分支预算调整为20秒，并增加独立的2200 token提交前输出上限。提交前提示不再携带仅供提交后Q34事实评分使用的说明；六项规则、知识内容、证据约束及提交后45秒/3000 token配置保持不变。前端整体等待仍为30秒，不增加自动重试。

0.12.8按三份反馈原始职责拆分调用：规则反馈保持原规则，知识库反馈直接使用当次实时知识快照中的受控标准，仅纯大模型反馈调用智谱。一次按钮从两次智谱调用降为一次，并增加30秒知识快照短缓存与并发请求合并；知识API或纯大模型真实失败时仍显示具体异常，不伪造成功反馈。模型并发4现可同时处理4次按钮检测。

0.12.9将提交前纯大模型改为智谱函数调用，由函数参数Schema约束六项结果，降低普通JSON模式偶发结构不合规的概率。同时知识客户端直接复用搜索接口已返回的完整记录；当三条TAORAN知识均命中时，一次快照只消耗1次知识API额度，不再追加三次详情请求。

0.13.0将按钮调整为同一接口内两任务并行：规则反馈只使用本地规则和已发布知识快照，不调用远程服务；知识库反馈实时检索知识并结合智谱模型分析。独立纯大模型反馈及其简道云字段回写取消。接口新增规则分支和知识库分支耗时字段，知识上下文限制为最相关的3至5条正式记录。

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

商业化接入可启用中文管理页`/admin/tenants`：网页可自动测试简道云连接、匹配字段、
原子更新注册表并立即重载。该功能默认关闭，详见[客户接入管理页.md](客户接入管理页.md)。
