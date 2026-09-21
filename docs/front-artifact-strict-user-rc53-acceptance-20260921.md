# Front Artifact 严格用户匹配与 Deep Review 闭环验收报告

## 1. 实际基线与范围

实施时服务器已高于方案草案中的 `rc48 / 1.0.9`，因此未回退覆盖新版，而是以当时真实最新版本为唯一安全基线：

- 隔离测试基线：`1.0.6rc50`，Commit `f967ddb`。
- 正式试点：`1.0.10`，全程不修改、不重启。
- 最终隔离候选：`1.0.6rc53`，Commit `596c61a`。
- 分支：`codex/front-artifact-strict-user-hardening-20260921`。
- Tag：`submit-test-v1.0.6rc53-20260921`。
- 未合并正式 `main`，未修改正式简道云，未修改 Q33/Q34 规则或权重。

## 2. 修改文件

- `src/taoran_agent/storage.py`：取消跨用户兜底，加入记录绑定、安全认领、元数据级不匹配统计和过期清理。
- `src/taoran_agent/front_analysis_artifact.py`：加入可信用户判断、Schema 白名单、底稿/整体大小限制，将业务证据纳入指纹，并在无 `front_review` 时从已校验改善建议生成最小结构化 finding。
- `src/taoran_agent/deep_review.py`：严格身份/记录/Schema 校验，新增 `unresolved`，Formal 未评价不再误算为 `corrected`。
- `src/taoran_agent/api.py`：Formal 评分路径不再传入 Front Artifact；Deep Review 结果在评分后进入正式反馈生成。
- `src/taoran_agent/feedback.py`：Formal 事实始终权威，对 deepened/corrected/new finding 进行去重融合，内部标签不向销售显示；构建异常时回退原 Formal feedback。
- `tests/test_front_analysis_artifact.py`：新增严格用户、记录绑定、评分隔离、Schema、TTL、反馈闭环、兼容 finding 和无改善项误识别等专项用例。
- 版本、锁定文件及隔离镜像构建文件同步到 `1.0.6rc53`。

## 3. 严格用户与记录匹配

Artifact 只有在以下条件全部成立时才可使用：

`tenant相同 + 可信user精确相同 + input_hash相同 + 已确认 + 未过期 + 未绑定或绑定当前记录`。

跨用户唯一 Artifact 也绝不会被读取或进入 Deep Review。跨用户诊断只返回数量，不读 payload。

不可信用户值包括：空值、`anonymous`、`unknown`、`unknown-user`、`system`、`jiandaoyun-user`、`jiandaoyun-submit-event`。身份不可信时直接独立 Formal Review。

Artifact 首次使用可原子绑定当前正式记录；已绑定 A 记录的 Artifact 不能被 B 记录复用。

## 4. 评分隔离

正式评分调用固定为：

`agent.evaluate(request, job_id)`

不再传入 `front_analysis`。Front Artifact 只在 Formal semantic facts 及 Q33/Q34 分数已产生后参与 reconciliation 和反馈组织。

`FrontSensitiveAgent` 专项测试已确认：评分 Reviewer 永远收到 `front_analysis=None`，有/无 Artifact 的 Q33、Q34 和总分一致。最终 10 条真实样本与同一输入的 rc51 Formal 基线分数也为 10/10 一致。

## 5. Feedback 闭环

`confirmed / deepened / corrected / new_finding / unresolved` 已作为正式 feedback builder 的输入，不再只停留在 diagnostics。

- Formal 事实与结论优先。
- Front 正确方向用 Formal 更深信息替代。
- Front 冲突内容不保留，仅显示 Formal 结论。
- Formal 新发现自然进入本次分析或改善建议。
- 不展示 Artifact ID、Hash、内部分类或规则标签。
- 未增加第三次模型调用。

## 6. 数据、Schema 与保留期

- 仅支持 `front-analysis-artifact-v1`，其他版本返回 `schema_incompatible` 并独立 Formal Review。
- Decision ledger 仅保留稳定字段，删除 Raw/Rejected/Repair 过程；ledger 上限 16 KiB，Artifact 上限 48 KiB。
- `evidence_ids` 代表销售上传的业务证明材料，不是纯平台波动字段，因此已纳入业务指纹。
- Store 初始化及明确维护方法会清理过期 Artifact，不需要新的后台守护进程。

## 7. 自动测试

- Python 全量：`693 passed`，1 条第三方 Starlette 弃用警告。
- Browser/测试插件：`82 passed`。
- Front Artifact/Deep Review 专项：`28 passed`。
- 本次修改文件 Ruff：通过。
- Compose 校验、镜像构建、内外网健康、数据库完整性、任务队列与管理页访问控制：通过。

## 8. 隔离真实样本验收

最终 `rc53` 使用测试表 10 条现有真实记录，并发 2，每条执行提交前快速分析、确认、提交后 Formal Review 和测试表回写。

- Front 完成：10/10；完整内容：10/10；确认接口 200：10/10。
- Backend 完成：10/10；回写成功：10/10；全流程成功：10/10。
- Artifact 严格命中：10/10，身份与记录匹配均为 true。
- Front 平均 7.29 秒，P95/Max 14.01 秒。
- Backend 平均 32.64 秒，P95/Max 39.80 秒。
- Reconciliation 合计：`deepened=13`、`corrected=1`、`new_finding=46`、`confirmed=0`、`unresolved=0`。
- 销售可见反馈内部标签泄露：0。

本批前端只对未达标/需改善项保留最小 finding，不会为未结构化的达标项臆造 `confirmed`，因此 `confirmed=0` 是保守分类，不是丢失正向反馈。

另在 `rc53` 使用 1 条真实记录专门构造“其他可信用户、相同内容、已确认 Artifact”：

- `front_artifact_status=user_mismatch`。
- `front_artifact_used=false`。
- `other_user_candidate_count=1`。
- Formal Review 和测试表回写仍正常完成。

这证明跨用户唯一 Artifact 不会被使用。

## 9. 发布与隔离证据

- 测试镜像：`taoran-submit-test:1.0.6rc53-20260921`。
- 镜像 ID：`sha256:7c07424b2c99b12dd44064e2d24ee0a9b99b7debc97e5b18cd330c8293925437`。
- Wheel SHA256：`da9a78b261f877c0950a33084d4dc4768c5a9d60486d253fd4ec042c609d58f8`。
- 测试容器：healthy，重启次数 0。
- 外网健康接口：`1.0.6rc53 / ok`，知识快照 usable=true。
- 数据库：`integrity_check=ok`，验收后无 queued/running 任务。
- 运行密钥配置哈希未变：`25356756fa6fc5afdba842634bbe6c8b6be329f2ce6b6c2dfd05a55ba5b3e6d9`。
- 正式容器仍为 `dsm-taoran-v2:1.0.10-feedback-recovery-20260920`，镜像 ID、启动时间和重启次数全程不变。
- 40题、知识库、飞书插件及其他容器未修改、未重启。

## 10. 回滚

最终发布前完整备份：

`/TAORAN agent/isolated-submit-test-20260916/backups/before-1.0.6rc53-20260921-20260921-112403`

备份包含当时的 Compose、运行配置和 SQLite 一致性备份。回滚只允许操作 `taoran-submit-test-agent`，目标为 `1.0.6rc52`。
