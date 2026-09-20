# Front Analysis → Backend Deep Review 最终隔离验收记录

## 最终候选

- 当前隔离测试版本：`1.0.6rc48`。
- 代码提交：`3270dcc`。
- 分支：`codex/front-artifact-deep-review-test-20260920`。
- 标签：`submit-test-v1.0.6rc48-20260920`。
- 镜像：`taoran-submit-test:1.0.6rc48-20260920`。
- 镜像ID：`sha256:45f91adfb612b98834b582267d92bf8ca40f2a4c344db2f5a376a0836f63c927`。

`1.0.6rc47`完成首次隔离发布后，在发布复核中发现“同一用户、相同内容有多次已确认成果”这一历史边界。`rc48`已调整为选择该用户最新一次已确认成果；跨用户兜底仍要求结果唯一，避免串用分析。

## 验收结果

- Python自动测试：`662 passed`。
- 浏览器与测试插件：`82 passed`。
- Front Artifact专项测试：`11 passed`。
- 本次修改文件Ruff：通过。
- 外网测试健康接口：200，版本`1.0.6rc48`。
- 隔离测试容器：健康，重启次数0。
- 数据库完整性：`ok`。
- 评价及Quick Check活动任务：均为0。
- 正式服务仍为`1.0.9`，正式容器ID未变化、重启次数0。
- 正式简道云插件、表单、推送和评分规则均未修改。

## 回滚

最终隔离版本的回滚备份：

`/TAORAN agent/isolated-submit-test-20260916/backups/before-1.0.6rc48-20260920-20260920-130242`

回滚范围仅限`taoran-submit-test-agent`。完整架构、数据结构、降级策略和调用链说明见前一份rc47验收记录；本文件记录最终候选差异和最终发布证据。
