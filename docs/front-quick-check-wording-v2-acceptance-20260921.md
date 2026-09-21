# Front Quick Check AI意见 V2 — 隔离 RC 验收

## 范围与基线

- Worktree：`/Users/ydzj/Documents/taoran-front-quick-check-v2`
- Branch：`codex/front-quick-check-wording-v2-20260921`
- 基线 Commit：`5cbc4116546425426e0ae2d0d66948bb5efa0075`；开始时工作区干净。
- 基线 Package / 隔离服务：`1.0.6rc60`。
- 最终 Package：`1.0.6rc62`；部署代码 Commit：`7d1178b8ec2aae26a7f2e015ca936e969769ed0b`。
- 基线已包含此前语义修复和 Backend Salesperson Wording V2；Backend Wording 仍有独立 worktree/分支，本轮未修改它们。
- 实测正式服务为 `1.0.10-feedback-recovery-20260920`，不是方案旧文档所称的 1.0.9。正式服务保持原样。

## 实际实现

实际 Quick Check 是“两阶段分析/建议 → 完整 Front Artifact → 流式/轮询展示”。建议阶段有时没有六项 sections，Artifact 会从完整建议补充 findings。本次兼容这一结构，没有强行新增上游模块或修改结构化事实契约。

新增 `front_quick_check_wording_v2.py`，仅在 `environment=isolated-submit-test` 且 `submit_confirmation_enabled=true` 时应用展示投影。先保存原有完整 Artifact，再生成用户可见内容。原始分析和建议在内部继续按原流程生成、校验和保存。

- 分析选取最多两句有业务意义的原文证据，保留说话人、否定、时间和计划状态；不截取前 N 字，不拼造客户事实。测试标记、空标题不作为业务事实展示。
- 只为影响本次修改的事项生成建议，按事实/来源错误、关键结果与自评、下一步、一般字段排序，最多三条。
- 正确客户类型、方式、自评和时间周期不逐项输出。没有待修改内容时只保留简短说明。
- 联系时间分 `agreed / planned / undetermined / unknown`。仅有字段空值不会推导“漏填”或“尚未约定”；未确定时不预填，销售计划不提升为客户共识。真实约定优先，展示层不强制跨月或跨季度。
- 宽泛/占位目标保持 `unresolved`；已有承诺和进展仍原文保留。明确 outcome 只能来自已校验分析/结构化 finding，展示层不以关键词启发式重新判定目标完成。
- 自评一致静默；存在明确偏差或目标事项待确认时才提示核对。目标宽泛时先具体化目标，不要求改为未达到。
- 每条建议带 `suggestion_basis`（来源、字段、原文证据），保存在 Quick Check outcome 的展示审计中；不写进 Artifact 事实层。
- 下一步只帮助明确已有计划；设备清单承诺可支持清单跟进的表达，不会凭办公桌需求生成报价、供货等新任务。
- 接口未收到字段放在“系统提示”，不算作业务建议。Footer 不参与建议证据或 finding。
- 按用户浏览器批注，提示改为：“记录尚未提交。阅读AI意见后可确认提交；返回修改或直接关闭均不提交。”

## 修改文件

业务运行文件只有：

1. `src/taoran_agent/front_quick_check_wording_v2.py`：独立展示投影。
2. `src/taoran_agent/api.py`：仅 `_quick_check_run` 增加展示出口接入；先持久化完整 Artifact。
3. `src/taoran_agent/interactive_quick_check.js`：删除用户指定的“不要求全部达标”提示。

其余是版本元数据、专项测试、隔离发布与验证工具：`pyproject.toml`、`uv.lock`、`src/taoran_agent/__init__.py`、`tests/test_front_quick_check_wording_v2.py`、`deploy/Dockerfile.submit-test`、`deploy/release_front_quick_check_v2.py`、`deploy/validate_front_wording_v2.py`、`deploy/compare_front_wording_v2.py`，以及本报告/发布清单。

## 自动与浏览器验证

- Python：**762 passed**，包含新增 24 项专项用例。唯一 warning 为原有 Starlette/httpx 弃用提示。
- Node 浏览器/插件：**82 passed**，覆盖 Interactive Browser、Submit Test Plugin、Content Cache Plugin。
- Ruff、wheel 构建、`git diff --check`：通过。
- 全量 Python 覆盖 Front V46、Quick Check、Artifact、Deep Review、Backend Wording、Writeback、Formal Evaluation；未修改其已有期望来绕过失败。
- 专项验证完整六项 finding / 四条原始建议在展示最多三条后仍完整保留；Deep Review 使用的 Artifact payload 除生成时间外与原构建结果一致。
- 真实隔离浏览器已观察：两阶段内容正常显示；完成前确认按钮禁用，完成后可用；未确定时间提示“不需要预设日期”；修复了联系时间缺失牵连具体下一步结果的多余建议；用户要求删除的提示已消失。
- 未点击确认提交，未触发正式或测试业务记录的提交/回写。独立打开页面没有简道云父窗口；父窗口回填/取消协议由原有自动测试验证，不能将独立页面观察说成简道云父窗口全流程提交验收。

## 30 条冻结输入对比（展示层隔离验收）

使用上一轮相同 30 个 data_id 和冻结 visit 输入，没有新造样本。原反馈来自 `real30-rc58-results.json`，基线 rc60 的 Front V46、Artifact、JS 及 `_quick_check_run` 与 rc58 对应逻辑一致；后续 rc59/rc60 改动为 Backend Wording。因此这是同一 Front 实现的已完成反馈留档对比，**不是本轮 30 次实时模型生成全部成功的声明**。原 30 条数据本身含已有隔离测试记录。

| 指标 | 原反馈 | V2 |
|---|---:|---:|
| 平均总长度（含原 Footer） | 267.27 | 185.00 |
| 平均正文长度（均排除 Footer） | 245.27 | 185.00 |
| 平均编号建议数量 | 1.47 | 2.17 |
| “原目标为/原定目标”命中记录 | 10 | 0 |
| 基础字段机械复述命中记录 | 5 | 0 |
| 自评一致/相符/合理命中记录 | 6 | 0 |
| 明确未约定时间仍强制补日期 | 2 | 0 |
| 无依据创造下一步销售动作 | 0 | 0 |
| 要求补写原文未出现的价格/交期反馈 | 2 | 0 |
| 新增虚构客户事实 | — | 0 |

正文平均缩短 **24.57%**；含旧 Footer 的总长度下降 **30.78%**。旧自评肯定 6 条中有 1 条是宽泛目标下不应确认的自评，其余 5 条属于可静默的肯定。编号建议变多是旧版常将日期和下一步合在同一个编号，不能将编号数当成独立问题数；V2 每条最多三项，重复内容已合并。

- 30/30 原文证据可逐字定位；本轮逐条审核 Grounded 30/30、Actionable 30/30。Actionable 指有可执行的实际修改/核对项，或完整记录没有凭空制造修改任务；这是本轮代理逐条判读，非独立人工或外部评审。
- 30/30 Artifact payload 在展示投影前后不变。
- 30/30 用同一份已冻结正式语义事实计算的 Q33/Q34/Total 在展示前后不变。没有重新调用 Backend 模型评价质量。
- V2 未确定时间强制填日期、虚构客户事实、无依据创造业务动作、宽泛目标强行判 achieved/not_achieved 均为 0。
- 逐条原文、V2、basis、输入哈希、评分与完整性检查见工作区 `outputs/comparison30.json` 与 `outputs/comparison30.md`。

## 实时模型重跑与限制

本轮额外通过真实隔离服务配置调用 Front worker，使用同一批冻结输入，保留所有失败，不触发 Backend 或 Writeback。

rc60 基线首轮前两条失败，补测后 29/30 完成；`BFJL2026092000351` 持续 `final_analysis_unavailable`，其建议阶段没有模型尝试，诊断为 `unknown_failure`。rc61 首轮同样出现前两条失败。该问题存在于原完整反馈生成阶段；本次没有绕过失败、伪造成功或改变 Artifact/正式分析契约。

最终 rc62 实时重跑 **29/30 成功**，与 rc60 补测后的同一条失败完全对应；30/30 输入哈希相同。成功配对的 29 条正文平均 **241.24 → 175.52 字（下降 27.24%）**，29/29 证据逐字可定位，29/29 线上展示结果可由同一完整 Artifact 和本地投影代码复现。失败条目不计入长度和 Grounded 成功率分母。

最终结果见 `outputs/live-rc62.json`、`outputs/live-comparison30.json`。**展示层验收通过；实时完整生成链保留一个原有失败，不能标为全量通过。** 未为让统计通过而改动原知识/模型生成链或正式语义规则。

规则投影对未识别的复杂目标/时间表达保留 `unresolved/unknown`，会比模型更保守。若完整上游模型不可用，仍沿用原失败/重试展示，不使用模板冒充成功的完整模型分析。

## 正式生产未受影响证据

- 108 个已有核心文件逐字节与基线一致，含 Backend Deep Review、Backend Wording、Formal Semantic Review、评分、Artifact 匹配和 Writeback。`api.py` AST 比较只改变 `_quick_check_run`。
- 正式容器 `dsm-taoran-v2-agent` 镜像 ID 始终为 `sha256:d9945f50d8bcc3e0124608859b524e752c184469b91034cd0090fb03aae5247d`。
- 正式启动时间始终为 `2026-09-20T13:38:32.844965384Z`，RestartCount 始终为 0。
- 正式 compose/runtime 文件哈希前后相同，正式数据库只读 `quick_check=ok`。
- 部署只执行隔离 compose 的 `up -d --no-deps --pull never agent`；未调用正式简道云回写，也未修改正式表单。
- 观测期间另一个飞书插件容器发生变化，因此这里只证明 TAORAN 正式服务边界，不声称整个服务器所有容器在整个观测期都未变化。
- 原始证据：`outputs/code-boundaries.json`、`production-before.json`、`production-after.json`、`protection-summary.json`、`deployment-rc62.json`。

## 隔离镜像与回滚

- 最终镜像：`taoran-submit-test:1.0.6rc62-20260921`。
- 镜像 ID：`sha256:1c2e1b4987b064753c826a27948d3d1b9042c50c44a4f483777e4af7b2e14658`。
- 只替换 `taoran-submit-test-agent`；内外网 health=ok，RestartCount=0。
- 本轮初始代码回滚点：`5cbc4116546425426e0ae2d0d66948bb5efa0075`。
- 本轮初始隔离镜像：`taoran-submit-test:1.0.6rc60-20260921`，镜像 ID `sha256:4b09ee0968f0936534b2f60dfc1ff6d7b77f10b68e91512f200eded9c02271fc`。
- 回到 rc60 的备份与脚本：`/TAORAN agent/isolated-submit-test-20260916/backups/before-1.0.6rc61-20260921-front-wording-v2/rollback.sh`。
- 上一临时候选 rc61 的备份另保存在 `backups/before-1.0.6rc62-20260921-front-wording-v2/`。
- 备份包含 compose、runtime 和 SQLite 一致性备份。常规回滚仅恢复隔离镜像/compose，不覆盖新增业务数据。正式服务无需回滚。
