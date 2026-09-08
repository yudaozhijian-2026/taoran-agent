# 0.27.13rc1 服务端监控

合并主分支0.27.12rc1已保存记录实时建议更新，本次另外仅增加前端最终分析请求的私有服务端日志。不改变提示词、输出结构、模型、超时、重试或评分规则，不修改插件参数。

日志路径：数据库同目录的model-failure-evidence/model-*.json，stage=model_transport。文件权限0600。包含源内容SHA256、开始时间、TCP连接、TLS握手、请求体发送完毕、响应头返回、首内容与完整生成耗时、供应商头部请求ID及正文响应ID。只保存白名单指标，不保存请求正文、认证头、URL或异常正文。

响应头和首内容时间均从请求开始累计，不能相加；TCP阶段可能包含DNS耗时，不能据此独立判定DNS耗时。无连接事件保持null，并标记reused_or_not_observed，不能直接视为0毫秒或确定复用。供应商未返回ID时保持null，不伪造ID。超时或网络失败也保留已获取的头部和连接阶段。日志落盘失败不会阻断原检测。

底层事件参考：https://www.encode.io/httpcore/extensions/#trace 。锁定依赖httpcore 1.0.9。监控仅针对此前50秒慢请求所在的frontend_final路径，实时Preview与后端深度评分保持现有监控；不将本次日志写入销售可见AI意见。

上线需使用release_transport_probe.py完成备份、版本防覆盖、空闲队列校验及仅TAORAN容器更新，再针对BFJL2026082500026进行新请求复测。

## 2026-09-08 发布结果

- 源码提交：cf7cccbde2ecdfec243776094a29ec0548f61096，已合并远端main。
- 发布标签：0.27.13rc1-transport-probe-20260908；包含已上线0.27.12rc1实时建议改动。
- 镜像：sha256:fe8816b7d49893337c038589af355b2cd2eae98a18a126b24ae5a7d311f7dafb。
- 408项Python测试、50项浏览器逻辑测试、Ruff、Compose及离线构建通过。
- 首次发布检查发现模型请求活跃，等待空闲后才备份切换，未中断模型请求。
- 97项源码哈希一致，数据库完整，983项评价任务及各表内容摘要不变；配置哈希、其他容器状态不变，容器重启次数0；内外健康与管理页正常。
- 发布清单：/TAORAN agent/releases/0.27.13rc1-transport-probe-20260908/deployment.json。
- 回滚：/TAORAN agent/backups/before-0.27.13rc1-transport-probe-20260908/rollback.sh（回到0.27.12rc1）。
- 本次未修改简道云插件配置。

## rc1复测与rc2日志口径修正

原第14条BFJL2026082500026在rc1首次复测：实时首段2.667秒、实时完整4.733秒、最终完整20.511秒；TCP80毫秒、TLS22毫秒、请求体发送完成103毫秒、响应头/首内容7883毫秒、完整生成17871毫秒。响应ID为20260908173100beba7152ebfa455f；供应商未返回头部请求ID。未复用缓存，未提交或回写记录。

发现httpcore在收到SSE结束标记后主动关闭生成器也报告receive_response_body.failed/GeneratorExit。rc2将该事件记为stream_reader_closed，不误记failed_phase；真实传输异常仍保留failed_phase。409项Python测试、50项浏览器逻辑测试、Ruff和Compose通过。该修正只影响私有监控字段，不改变检测内容。

## 当前正式发布：0.27.13rc2

- 正式源码提交cffd734dd53a7348d188b6475dc974833877759a，已回归远端main。
- 唯一Git/镜像标签0.27.13rc2-transport-probe-20260908。
- 镜像sha256:acfaac0b19f25762233f5d333107bd15277849678b79d1f359f4dc46cc8fe8ae。
- 更新前检测到新的用户任务，未强制切换；等待完成并重新备份最新数据后发布。
- 发布后985项评价任务及其他表摘要一致、完整性ok；97项源码哈希一致、管理页与内外健康正常；运行配置及其他容器不变，重启0。
- 当前发布清单/TAORAN agent/releases/0.27.13rc2-transport-probe-20260908/deployment.json。
- 当前回滚路径/TAORAN agent/backups/before-0.27.13rc2-transport-probe-20260908/rollback.sh，回到rc1。rc1回滚路径仍可用于返回0.27.12rc1。

### rc2案例复测

BFJL2026082500026：任务qc_1be2ff569020442c933ab2a56c19083b；实时首段2.104秒、实时完成4.197秒、最终完成19.481秒。TCP7毫秒、TLS31毫秒、请求体发送完成39毫秒；响应头/首内容3687毫秒，完整模型生成16783毫秒，随后语义复核1563毫秒。响应ID202609081736137804f5c380e34da9，头部请求ID仍未返回。前端状态completed、content_complete=true，单次生成、未复用缓存；正常SSE关闭标记正确，无failed_phase。

两次复测source_hash一致；原50.610秒未复现。本次只增加监控，没有证明延迟已稳定优化。复测为实际模型接口检测，不重新提交记录、不确认回填。原始结果保存在outputs/taoran-case14-transport-20260908/retest-rc1.json与retest-rc2.json。
