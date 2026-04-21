# Official Group Webhook Bridge Schema v1

更新时间：2026-04-21
协议版本：`official-group-webhook-v1`

## 1. 用途
用于 `WebhookOfficialGroupApprovalExecutor` 与外部官方群审批 bridge / webhook 之间的协议约定。

目标：
1. 让 Hermes 后端可以把“已通过 CRM eligibility gate 的官方群审批请求”发给外部 bridge
2. 让 bridge 用统一的状态语义回传执行结果
3. 支持：
   - 成功
   - 可重试失败
   - 需人工继续
   - 一般失败

---

## 2. 请求
Webhook URL 由以下配置提供：
- `OFFICIAL_GROUP_APPROVAL_WEBHOOK_URL`
- `OFFICIAL_GROUP_APPROVAL_WEBHOOK_TOKEN`

请求方法：
- `POST`

请求头：
- `Content-Type: application/json`
- 如配置 token：
  - `Authorization: Bearer <token>`

请求体：
```json
{
  "target_group": "official-group-a",
  "lead": {
    "lead_id": "lead_xxx",
    "mobile": "89999999999",
    "yw_id": "66778899",
    "app_name": "Linky",
    "dept_name": "Piso",
    "pendaftaran_group": "Piso-5"
  },
  "crm_snapshot": {
    "id": "crm_123",
    "mobile": "89999999999",
    "ywId": "66778899",
    "appName": "Linky",
    "deptName": "Piso",
    "pendaftaranGroup": "Piso-5",
    "wa": "",
    "joinGroup": 0
  },
  "task": {
    "task_id": "task_xxx",
    "lead_id": "lead_xxx",
    "task_type": "group_join",
    "status": "pending"
  }
}
```

说明：
- `lead` 为当前本地 lead 快照
- `crm_snapshot` 为审批前 CRM eligibility gate 查到的客户快照
- `task` 为当前官方群审批对应的 `group_join` 任务快照

---

## 3. 响应
外部 bridge 必须返回 JSON object。

基础字段：
```json
{
  "status": "success|retryable_failed|manual_required|failed",
  "result_code": "string",
  "result_reason": "string",
  "raw_result": {
    "target_group": "official-group-a"
  }
}
```

### 3.1 success
表示官方群审批已完成。

示例：
```json
{
  "status": "success",
  "result_code": "approval_ok",
  "result_reason": "approved by upstream bridge",
  "raw_result": {
    "target_group": "official-group-a",
    "bridge_request_id": "req_123"
  }
}
```

### 3.2 retryable_failed
表示这次执行失败，但适合技术重试。

适用场景：
- bridge 超时
- 临时网络错误
- 上游服务 5xx
- 临时限流

示例：
```json
{
  "status": "retryable_failed",
  "result_code": "upstream_timeout",
  "result_reason": "bridge timeout",
  "raw_result": {
    "target_group": "official-group-a",
    "bridge_request_id": "req_124"
  }
}
```

Hermes 侧归一化后：
- 顶层 `status` 会按既有 `group_join_result` 兼容逻辑落成 `failed`
- 但 `raw_result` 会补：
  - `execution_disposition=retryable_failed`
  - `retryable=true`
- decision API 顶层会返回：
  - `follow_up_action=retry_official_group_approval`
  - `retryable=true`

### 3.3 manual_required
表示这次不能自动完成，需要人工继续。

适用场景：
- 需要验证码
- 需要人工确认登录
- 需要人工点确认
- 平台风控要求人工处理

示例：
```json
{
  "status": "manual_required",
  "result_code": "captcha_required",
  "result_reason": "captcha required by upstream bridge",
  "raw_result": {
    "target_group": "official-group-a",
    "bridge_request_id": "req_125"
  }
}
```

Hermes 侧归一化后：
- 顶层 `status` 会落成 `failed`
- `raw_result` 会补：
  - `execution_disposition=manual_required`
  - `requires_human_action=true`
- decision API 顶层会返回：
  - `follow_up_action=manual_continue_official_group_approval`
  - `requires_human_action=true`
  - `human_action_type`（当前按 `result_code/result_reason` 推断）

### 3.4 failed
表示普通失败，不建议技术自动重试。

适用场景：
- 用户不符合资格
- 目标群配置错误
- 数据被拒绝
- 上游明确拒绝

示例：
```json
{
  "status": "failed",
  "result_code": "not_eligible",
  "result_reason": "user is not eligible for this official group",
  "raw_result": {
    "target_group": "official-group-a"
  }
}
```

Hermes 侧表现：
- `follow_up_action=queue_reengagement` 或按后续更细规则扩展

---

## 4. 当前 Hermes 侧 follow-up 规则
若 decision API 调用 executor 后返回：

1. `success`
- `follow_up_action=close_or_education`

2. `retryable_failed`
- `follow_up_action=retry_official_group_approval`
- `retryable=true`

3. `manual_required`
- `follow_up_action=manual_continue_official_group_approval`
- `requires_human_action=true`
- `human_action_type` 目前根据 `result_code/result_reason` 推断：
  - 包含 `captcha` -> `captcha_required`
  - 包含 `auth/login` -> `auth_required`
  - 包含 `session/expired` -> `session_expired`
  - 否则 -> `manual_continue_required`

4. 其他 `failed`
- `follow_up_action=queue_reengagement`

---

## 5. 对接建议
对接真实 WhatsApp 官方群 bridge 时，建议至少保证：
1. 响应一定是 JSON object
2. 一定带 `status`
3. 一定带 `result_code`
4. 一定带 `result_reason`
5. `raw_result` 中尽量带：
   - `bridge_request_id`
   - `target_group`
   - 上游状态码 / 错误详情

这样可以让 Hermes 侧后续补：
- retry API
- manual continue 工作台
- executor health 更细指标
- CRM 审批统计同步
