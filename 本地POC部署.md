# TAORAN本地POC部署记录

更新时间：2026-08-19

## 当前访问地址

- 本地服务：`http://127.0.0.1:8030`
- 临时HTTPS地址：`https://nasa-blink-may-myth.trycloudflare.com`
- 健康检查：`https://nasa-blink-may-myth.trycloudflare.com/health`
- AI检测接口：`https://nasa-blink-may-myth.trycloudflare.com/api/v1/connectors/jiandaoyun/visit/button-check`
- 深度评价接口：`https://nasa-blink-may-myth.trycloudflare.com/api/v1/connectors/jiandaoyun/visit/evaluations`

该地址使用Cloudflare Quick Tunnel，仅用于POC。电脑关机、服务停止或隧道重启后，HTTPS地址可能变化；变化后必须同步更新
简道云测试副本的前端事件地址。正式试运行应改为具有固定域名的命名隧道或正式服务器。

## 当前运行方式

在项目根目录启动Agent：

```bash
uv run taoran-agent serve --host 127.0.0.1 --port 8030
```

另一个终端使用`cloudflared`把本机8030端口映射为HTTPS地址。两个进程必须同时运行，本机需要保持联网和唤醒。

## 已完成验证

- HTTPS健康检查返回200；
- 未携带TAORAN服务密钥访问受保护接口返回401；
- 正确密钥访问字段映射返回200；
- Q40预留接口保持关闭；
- 真实简道云记录可以通过HTTPS调用AI检测，返回`can_submit=true`；
- 专用记录`BFJL2026081900001`完成深度评价与双字段回写；
- 回写结果：Q33为100、Q34为0、总分为100；
- 简道云读回AI评分为`100`，AI反馈意见非空；
- 回写字段严格限制为AI评分和AI反馈意见。
- 简道云测试副本的“AI检测”按钮已保存并关联1个启用的TAORAN前端事件；
- 前端事件返回值仅回填AI反馈意见，不在提交前写入AI评分；
- 前端事件字段选择器未提供“过程详细描述”，当前提交前检查不传该项；提交后深度评价继续通过V5 API获取完整记录。
- 专用记录实际点击“AI检测”后，服务返回HTTP 200，AI反馈意见约3.4秒完成回填；
- 实测期间提交按钮始终可用；服务未运行时调用失败也未阻断提交；
- 已兼容简道云空字段字符串`"null"`、`"undefined"`和空字符串，34项自动化测试通过；
- 实测仅验证表单内即时回填，未点击提交，未覆盖记录中已保存的深度评分和反馈。

## 下一阶段

- 接入提交后异步深度评价触发；
- 验证提交后AI评分和AI反馈意见双字段自动回写；
- Quick Tunnel仅用于当前POC，正式试运行前改为固定HTTPS域名和受守护的常驻服务。

## 安全约束

- 简道云API Key和TAORAN服务密钥只保存在`.env`，不得写入本文档、代码、样例或日志；
- 字段映射查看接口已增加租户认证；
- Quick Tunnel地址本身不作为认证凭据；
- 测试结束后停止隧道即可立即撤销公网入口；
- 正式试运行前需要增加稳定域名、进程守护、日志轮转、限流和可恢复任务队列。
