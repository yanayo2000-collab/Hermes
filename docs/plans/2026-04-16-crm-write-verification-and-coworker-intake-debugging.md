# 2026-04-16 CRM 写后验证与同事收口消息调试纪要

## 背景
在收口机器人链路中，出现了一个严重问题：
- 同事发送的消息，机器人可能回复 `✅ Success`
- 但 CRM 页面查询却看不到对应记录

用户要求的业务口径非常明确：
1. 每次收到人工客服提交的信息，都必须实际打一次 CRM 创建
2. 是否回复成功，只能由“本次 CRM 真实记录成功”来决定
3. 只要本次 CRM 最终不可验证，就绝不能回复成功

## 已确认事实

### 1. 同事消息并不是没有进入 CRM create
排查 `leads` / `account_submissions` / `sync_logs` 后确认：
- 同事 open_id 的消息会正常创建 lead/submission
- bind 模拟成功后，确实调用了 `create_customer`
- 说明问题不是“同事账号没有进入 CRM 写入逻辑”

### 2. 部分 create 返回 success，但 CRM page 查询不到
出现过如下情况：
- `sync_logs.response_snapshot.crm_response.code = 0`
- `msg = success`
- 但随后用 CRM `/customer/ywcustomer/page` 按 `ywId/mobile/appName/deptName/pendaftaranGroup` 查询，仍返回 `total = 0`

这意味着：
- create success 不能直接等价于“真实落库成功”
- 必须做写后验证

### 3. 重复提交场景也必须按本次 CRM 结果判定
用户确认的业务规则：
- 第一次提交成功 -> 可回复 `✅ Success`
- 第二次同样内容再提交 -> 仍需实际打 CRM create
- 如果 CRM 本次拒绝重复 -> 必须回复失败，不能因历史已有记录而回复成功

## 本次落地修复

### A. 收紧成功判定
当前成功判定改为：
- CRM create 返回 `code=0`
- 且 query-back 能查到与本次写入关键字段一致的记录

否则统一视为失败。

### B. 写后验证字段
query-back 匹配必须同时检查：
- `ywId`
- `mobile`
- `appName`
- `deptName`
- `pendaftaranGroup`

原因：
- 仅凭 `ywId/mobile` 的模糊命中不够安全
- 必须确认查回来的就是本次这条写入

### C. 失败模板归一
当前 CRM 失败相关模板：
- `**❌ CRM sync failed: CRM write was rejected.**`
- `**❌ CRM sync failed: Data duplication.**`
- `**❌ CRM sync failed: Please retry once.**`
- `**❌ CRM sync failed: CRM app mapping is missing. Please contact the administrator.**`
- `**❌ CRM sync failed: CRM write could not be verified.**`

## 额外业务规则整理

### 1. Group 格式
- 合法：`English-Number`，如 `Piso-12`
- 非法但像群组候选：如 `Piso12`
- 对非法候选应回复：
  - `**🚫 Invalid group format. Use English-Number, e.g. Piso-12.**`

### 2. App ID 规则
- `Linky`：ID 必须是 8 位数字
- `FUMI`：ID 也必须是 8 位数字

## 已验证通过的同事样本
已用同事 open_id 做过一条真实自测，并验证成功：
- 输入：
  - `77123456`
  - `+62 933112345`
  - `Piso-16`
- 机器人返回：
  - `**✅ Success**`
- CRM query-back：
  - 能查到完全匹配记录

## 仍在继续追查的问题
尽管已补齐“不能假成功”的保护，但 CRM 侧仍存在可疑现象：
- 某些 create 返回 success
- page 查询仍然不可见
- CRM 登录/下拉接口本身还频繁出现 502 / 非 JSON / 超时

因此后续仍需继续：
1. 比对“可见成功样本”和“不可见 success 样本”的差异
2. 继续验证 CRM page 是否就是正确的最终查询口径
3. 确保后续多人工客服并发使用时，所有成功回复都对应真实 CRM 可见记录

## 当前阶段结论
本地收口逻辑已经实现以下约束：
- 不再把单纯 `create_customer(code=0)` 当作最终成功
- 不再把写后无法验证的记录回复为成功
- 重复提交不再因历史已有记录而直接回复成功

下一阶段目标不是“减少错误提示”，而是继续把 CRM 真实打通，直到：
- 任意人工客服提交
- 机器人回 `✅ Success`
- CRM 页面必然能查到一致记录
