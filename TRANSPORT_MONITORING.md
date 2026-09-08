# 0.27.13rc1 服务端监控

合并主分支0.27.12rc1已保存记录实时建议更新，本次另外仅增加前端最终分析请求的私有服务端日志。不改变提示词、输出结构、模型、超时、重试或评分规则，不修改插件参数。

日志路径：数据库同目录的model-failure-evidence/model-*.json，stage=model_transport。文件权限0600。包含源内容SHA256、开始时间、TCP连接、TLS握手、请求体发送完毕、响应头返回、首内容与完整生成耗时、供应商头部请求ID及正文响应ID。只保存白名单指标，不保存请求正文、认证头、URL或异常正文。

响应头和首内容时间均从请求开始累计，不能相加；TCP阶段可能包含DNS耗时，不能据此独立判定DNS耗时。无连接事件保持null，并标记reused_or_not_observed，不能直接视为0毫秒或确定复用。供应商未返回ID时保持null，不伪造ID。超时或网络失败也保留已获取的头部和连接阶段。日志落盘失败不会阻断原检测。

底层事件参考：https://www.encode.io/httpcore/extensions/#trace 。锁定依赖httpcore 1.0.9。监控仅针对此前50秒慢请求所在的frontend_final路径，实时Preview与后端深度评分保持现有监控；不将本次日志写入销售可见AI意见。

上线需使用release_transport_probe.py完成备份、版本防覆盖、空闲队列校验及仅TAORAN容器更新，再针对BFJL2026082500026进行新请求复测。
