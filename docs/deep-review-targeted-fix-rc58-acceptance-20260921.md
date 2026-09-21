# TAORAN Deep Review 真实样本定向修复验收报告

## 1. 基线与范围

- Branch：`codex/deep-review-sample-fix-20260921`
- Commit：`6f35645279626405620f01bea3dcf1caf4dd7745`
- Tag：`submit-test-v1.0.6rc58-20260921`
- Package：`1.0.6rc58`
- 隔离镜像：`taoran-submit-test:1.0.6rc58-20260921`
- Image ID：`sha256:4c818b5d0257b429117d50e683a84980db9957ca1ed69ce514df9e1bf8635798`
- 只更新隔离容器：`taoran-submit-test-agent`
- 实际正式服务版本为 `1.0.10`，任务文本中的 `1.0.9` 已过时。本轮没有更改正式服务。
- rc58 完成30条验收后，隔离测试服务被另一轮任务升至 `1.0.6rc59`。本任务检测到更新版本后没有用 rc58 覆盖服务器；rc58 验收结果仍由冻结提交、镜像和服务器历史发布清单支撑。

## 2. 修改文件

业务修复文件：

- `src/taoran_agent/deep_review_gates.py`
- `src/taoran_agent/llm.py`
- `src/taoran_agent/feedback.py`
- `src/taoran_agent/writeback.py`
- `src/taoran_agent/post_repair.py`
- `src/taoran_agent/post_review_policy.py`
- `src/taoran_agent/experimental_semantic_streaming.py`
- `src/taoran_agent/experimental_semantic_streaming_v21.py`
- `src/taoran_agent/saved_record_check.py`

测试文件：

- `tests/test_deep_review_gates.py`
- `tests/test_post_repair.py`

发布准备及版本文件：

- `pyproject.toml`
- `src/taoran_agent/__init__.py`
- `uv.lock`
- `deploy/Dockerfile.submit-test`
- `deploy/release_token_usage.py`

未修改 `scoring.py`、`scoring_contract.py`、`taoran_rules_v1.json` 及正式评分权重。

## 3. 六类问题的根因与通用修复

| 问题 | 根因 | 通用修复机制 |
| --- | --- | --- |
| Final Feedback 截断 | 写回前只校验字段存在，没有检查句子和建议模块是否完整 | `Final Feedback Completeness Gate`，失败时先重试文案，仍失败则使用已验证构建器兜底 |
| 诱导补造已确认事实 | “补充”建议没有与原始正式记录进行否定/肯定证据绑定 | `Advice Truthfulness Gate` + `Suggestion Evidence Binding`，无证据时改为下一步确认或条件表达 |
| 模型创造新的公司强规则 | 语义建议没有来源分类，可被写成“必须/应当” | `Requirement Provenance Gate`，只允许确定性规则或知识政策使用强制语气 |
| 不可评估被误判为未达到 | 输出状态只有达到/未达到，没有保留不确定状态 | 增加 `unresolved`，并在反馈层禁止将其转成 `not_achieved` |
| 原目标质量差时否定已取得成果 | 目标质量与实际成果混合判断 | `Actual Outcome Preservation`，分开 `goal_quality` 与 `actual_outcome` |
| 客户承诺与已完成结果混淆 | 事件时态识别只看源文全文，已完成事实可被后文未来承诺污染 | `Commitment vs Completed Result Gate`，只按当前成果证据句判断时态 |

## 4. Golden Cases 与新增测试

本轮固化 11 个专项断言，覆盖：

1. 截断分析必须拒绝。
2. 完整分析+建议必须通过。
3. 无改善项的完整分析合法通过。
4. 不得把“负责人尚未明确”改写为“补充已确认信息”。
5. 语义建议不得伪装成公司强规则。
6. 不可评估必须保留为 `unresolved`。
7. 长句中的“无法核验→未达成”也必须拦截。
8. 宽泛目标下的数量、规格等实际成果必须保留。
9. 未来承诺不得当成已完成。
10. 已完成成果不得因后续承诺被重写为未来事件。
11. 真实未来承诺必须表达为有效进展但尚待完成。

## 5. 自动化回归结果

- Python：`712 passed`，1 条第三方库 warning。
- Browser：`67 passed`。
- Ruff：通过。
- Build：`uv build` 通过。
- Compose：配置校验通过。
- Wheel：`dsm_taoran_agent-1.0.6rc58-py3-none-any.whl`。

## 6. 同一30条真实数据回归

- 30/30 前端生成完成。
- 30/30 后端 Deep Review 生成完成。
- 30/30 Front Artifact 严格用户匹配和记录匹配通过。
- 22 条实际写回；8 条因记录已有前端反馈，按当前 `FRONT_FEEDBACK_ALREADY_PRESENT` 设计跳过写回。这8条的后端生成已完成，不属于生成失败。

耗时：

| 环节 | 平均 | P50 | P95 | 最大 |
| --- | ---: | ---: | ---: | ---: |
| 前端 | 8.066s | 5.668s | 24.343s | 27.492s |
| 后端 | 45.223s | 41.986s | 93.595s | 95.057s |
| 全流程 | 53.289s | 50.287s | 99.484s | 107.385s |

## 7. 修复前后问题数量

| 检查项 | 修复前 Backend W | 修复后 Backend New |
| --- | ---: | ---: |
| Final 截断 | 2 | 0 |
| 虚构/诱导补造事实 | 1 | 0 |
| Unsupported Requirement | 1 | 0 |
| `unresolved` 误判 `not_achieved` | 4 | 0 |
| 真实成果被错误否定 | 2 | 0 |
| 客户承诺误当已完成 | 0 | 0 |

修复前数量由同一30条的 Backend W 人工复核与定向规则扫描得出；修复后数量由同样本逐条复核得出。

## 8. Q33/Q34 评分一致性

- Q33/Q34 评分公式、权重、规则文件未修改。
- 与 rc53 旧结果相比，22/30 总分完全一致，8/30 总分发生变化。
- 8 条差异是语义判断输出变化造成，不是公式或权重变更。该项不宜在扩大样本前标记为“完全一致”。

## 9. Deep Review 五类统计

| confirmed | deepened | corrected | new_finding | unresolved |
| ---: | ---: | ---: | ---: | ---: |
| 0 | 28 | 3 | 101 | 13 |

候选版没有因证据边界变成“什么都不敢判断”：仍然保留了大量 deepened、corrected 和 new_finding。

## 10. P0/P1/P2 结论

### P0

- 截断正式反馈：0。
- 编造关键事实：0。
- 诱导补造事实：0。
- 跨用户 Artifact：0，30/30用户和记录匹配。
- Front 影响正式评分：0，Front Artifact 不参与评分。

### P1

- Unsupported Requirement：0。
- 不可评估误判未达到：0。
- 明确成果被错误否定：0。

### P2 遗留

1. 后端 P95 为 93.595 秒，严格门禁与必要重试提高了安全性，但延迟比修复前增加。
2. 8/30 评分发生变化，需业务抽审确认语义判断调整是否符合正式评分口径。
3. 8 条回写跳过是当前业务设计，扩大验收时要将“生成成功率”与“实际覆盖写回率”分开统计。

## 11. 隔离发布与回滚

- rc58 发布验证时：内外网健康接口均正常，容器重启次数0，数据库完整性 `ok`，活跃任务0，其他容器状态未变。
- 当前复核时：隔离外网健康接口已显示 `1.0.6rc59`，正式外网健康接口显示 `1.0.10`。为避免覆盖并发更新，本任务停止重新部署 rc58。
- 服务器发布清单：`/TAORAN agent/isolated-submit-test-20260916/releases/1.0.6rc58-20260921/deployment.json`
- 回滚路径：`/TAORAN agent/isolated-submit-test-20260916/backups/before-1.0.6rc58-20260921-20260921-145300`

## 12. 下一步建议

建议进入下一轮扩大样本验收，但不建议直接发布正式生产。扩大验收前先完成两项：

1. 对8条评分差异进行业务复核。
2. 在不降低证据安全门禁的前提下降低重试率和后端P95。

扩大样本时应以当时服务器最新候选版为基线重新合并本修复，不应回退覆盖 rc59。
