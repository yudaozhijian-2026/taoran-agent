# Front Quick Check V2.1 定向优化验收报告

1. **V2.1 基线 Branch**：`codex/front-quick-check-wording-v2-20260921`。
2. **基线 Commit**：`f8a38532d53e9411adcb6ced329093ce1e356dbd`，开工时工作区干净。
3. **新 Commit**：实现 `2940d3ce530a24d8afd72e820c5d187efef13bd5`，采购/实施事实边界修正 `73391d94e99c2f2a95a777abe504fd6540153e42`，最终隔离部署源 `13279294524d98d209a5312ddee85d0190b38257`。
4. **新 Version**：Package `1.0.6rc63`；展示投影 `front-quick-check-wording-v2.1-20260921`。
5. **修改文件**：`src/taoran_agent/front_quick_check_wording_v2.py`、`tests/test_front_quick_check_wording_v2.py`、`deploy/compare_front_wording_v21.py`、`deploy/Dockerfile.submit-test-v21`、`deploy/release_front_quick_check_v21.py`、`pyproject.toml`、`uv.lock`、`src/taoran_agent/__init__.py`，以及本报告和 rc63 发布清单。
6. **文件职责**：展示投影文件负责摘要、证据建议和时间分类；测试文件覆盖 12 类 Golden Case 及隔离边界；对比脚本只读冻结输入并生成三方比较；Dockerfile/发布脚本只替换隔离容器；版本文件顺延 rc63。
7. **消除“记录中的重点是”**：展示层不再拼接带引号原句，冻结 30 条由 V2 的 28 条命中降至 V2.1 的 0 条。
8. **原文摘要机制**：`analysis_basis` 继续保留可逐字定位的源句；展示文字按培训、设备实施、采购审批、办公桌、审核等已存在业务证据提炼“已取得什么＋仍不能确认什么”。采购事实不会转写成安装事实。
9. **Evidence-specific Suggestion**：建议优先绑定审批负责人、培训日期/参训人员、设备清单、电源准备、产品信息和审核节点。每条仍带 `suggestion_basis.source/evidence/fields`；冻结 30 条通用“按已有计划”由 13 降为 0。
10. **自评静默机制**：删除“存在待确认事项就提醒自评”的泛化分支。只有已校验 A2 明确冲突或明确 outcome 与自评不一致才显示自评建议；宽泛目标只提示先具体化目标。
11. **下一次联系时间四状态**：字段已填时正常静默，仅日期先后或明确绝对日期冲突才提示；字段为空时依次判断明确联系约定、客户未来事件、销售计划/明确未约定、完全无计划。
12. **confirmed_next_contact 判断**：结合主语、约定词、联系动作和时间上下文识别绝对日期、相对时间、事件后联系；支持“销售已与客户约定一周后再次沟通”“并再联系核对”“届时再联系核对”，不含 Case ID 特判。
13. **customer_event_date 与 next_contact_agreement**：只有客户承诺未来提供清单时标记 `future_customer_event`，不会把事件日期填作联系日期；存在“再联系/沟通/拜访/讨论”的约定语义才进入 `agreed`。
14. **客户类型频度 reference**：仅在字段为空且记录无联系安排时，潜力客户显示“可以参考跨自然季度”，目标客户显示“可以参考跨自然月”，并明确具体时间以实际推进为准；不会覆盖已填日期或真实约定。
15. **自动测试**：最终全量 Python `766 passed`；含 28 项 Front wording 专项断言，覆盖方案要求的 17 类边界。唯一 warning 是既有 Starlette/httpx 弃用提示。
16. **Ruff**：`ruff check .` 通过；`git diff --check` 通过；wheel/sdist 构建通过。
17. **Browser/Plugin**：Node `82 passed`，覆盖 Interactive Browser、Submit Test Plugin、Content Cache Plugin。未修改任何 Browser/Plugin 运行文件。
18. **30 条 Original / V2 / V2.1**：同一 `frozen30.json` 输入哈希 30/30 一致。平均反馈长度 `267.27 / 185.00 / 186.23`；平均建议数 `1.47 / 2.17 / 2.03`。逐条正文见 `outputs/comparison30-v21.md`，结构化数据见 `outputs/comparison30-v21.json`。
19. **明确联系约定漏识别**：0。`0349`、`0363`、`0364` 均进入 `agreed`；其中 `0349` 为相对时间，`0364` 为事件后联系。
20. **客户事件误判联系时间**：0。`0361/0362/0365–0369` 的清单交付均未转成联系约定。
21. **强制跨月/跨季度表达**：V2.1 均为 0；频度文字均使用“可以参考”，没有“必须/确保/不满足即不通过”。
22. **虚构事实**：0。30/30 展示建议证据逐字可定位；人工逐条核对未发现新增客户事实。
23. **无依据销售动作**：0。没有生成报价、供货方案、推动采购或额外预算任务；办公桌案例只承接记录中已有的产品了解与回复安排。
24. **Grounded**：人工逐条核对 30 PASS、0 REVIEW、0 FAIL；确定性 evidence exact match 30/30。
25. **Specific**：30 PASS、0 REVIEW、0 FAIL；通用建议模板在冻结样本中为 0。
26. **Actionable**：30 PASS、0 REVIEW、0 FAIL；每条建议均指向可核对字段或已有业务事项。
27. **Natural**：30 PASS、0 REVIEW、0 FAIL；不再使用搜索结果式原文引用。
28. **Time-advice-correct**：30 PASS、0 REVIEW、0 FAIL；联系约定漏识别、客户事件误判和无计划未提醒均为 0。
29. **Front Artifact 完整性**：投影前后 Artifact payload 30/30 相同；完整 findings/suggestions 先保存，再生成最多三条用户可见建议。`front_analysis_artifact.py` 与 V2 基线 blob hash 同为 `ba6c84c796c430e3694033364ee036a09365e72f`。
30. **Backend 未变化**：相对 V2 基线仅变更独立 Front 展示文件、测试、对比/隔离发布工具和版本元数据。`api.py`、Backend Salesperson Wording、scoring、writeback 均未改；对应 blob hash 与基线完全一致。
31. **Q33/Q34/Total**：冻结语义事实不变，展示投影前后 30/30 的 Q33、Q34、Total 完全相同。Formal Semantic Facts 只读复用，没有重新分析 Backend。
32. **隔离镜像**：`taoran-submit-test:1.0.6rc63-r2-20260921`，ID `sha256:8f4434618107effd1892204c1d4dd352e1d717d68dc75398e9057672e2132435`，revision `13279294524d98d209a5312ddee85d0190b38257`。
33. **容器健康**：`taoran-submit-test-agent` 内外网 health 均为 `ok`，release `1.0.6rc63`，RestartCount 0，活动任务 0，隔离数据库 integrity `ok`。
34. **正式生产未受影响**：正式容器始终为 `dsm-taoran-v2:1.0.11-present-date-feedback-20260921`，镜像 ID `sha256:ff8b74e7dbfeb47edd7a855613cfc664c22e65c12afe4085cb90e4c125ada356`，StartedAt `2026-09-21T08:43:55.514390681Z`，RestartCount 0，healthy，数据库 integrity `ok`。隔离发布脚本验证其他容器逐项未变；未调用正式 compose、Writeback 或简道云字段接口。
35. **回滚**：代码回滚点为 V2 基线 `f8a38532d53e9411adcb6ced329093ce1e356dbd`。立即回到 rc63 首候选可执行 `/TAORAN agent/isolated-submit-test-20260916/backups/before-1.0.6rc63-r2-20260921-front-wording-v21/rollback.sh`；完整回到 V2 rc62 可执行 `/TAORAN agent/isolated-submit-test-20260916/backups/before-1.0.6rc63-20260921-front-wording-v21/rollback.sh`。
36. **遗留问题**：最终隔离实时完整链重跑 29/30 成功；`BFJL2026092000350` 在完整 Front 生成阶段持续 `unknown_failure`，模型尝试数为 0。rc62 同批也为 29/30，但失败项为 `BFJL2026092000351`，所以实时共同成功配对 28 条。29 条 rc63 成功结果全部 Grounded 且可由最终 Artifact＋部署投影复现。确定性三方展示比较覆盖全部 30 条并通过全部验收指标。该遗留属于既有上游生成稳定性，本轮未绕过失败、未增加模型调用、未修改知识/语义/Backend 链路。

结构化发布证据见 `deploy/release-manifest-1.0.6rc63-20260921.json`。本轮运行时代码没有新增 LLM 调用；实时验收复用了既有 Front worker，只写隔离测试库，不确认提交、不触发 Backend 或 Writeback。
