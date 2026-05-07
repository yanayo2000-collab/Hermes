# WhatsApp 官方群自动审批 MVP：最终技术清单与差距表

更新时间：2026-04-21

## 1. 目标定义（按最新业务口径修正）
MVP 不是只做 `group_join_result` 回写，而是要形成这条业务闭环：

1. 客服/系统完成收口
2. 公会后台 bind success
3. CRM create / update success + verify success
4. 用户进入官方群审批候选池
5. 审批前核对 CRM 用户数据
6. 符合条件才同意加入 WhatsApp 官方群
7. 审批动作、结果、人数变化进入统计
8. CRM 里保留进群人数 / 审批结果 / 官方群状态记录

补充约束：
- 注册群 `registration_group` 与官方群 `official_group` 是两条不同链路
- CRM 不是 bind 前置闸口，但它是官方群审批前的 eligibility gate
- 一次人工发送 = 一次 submission
- 只有本次 bind + CRM verify 成功，才允许进入官方群自动审批

---

## 2. 最终技术清单（MVP 必须具备）

### A. 候选池与前置闸口
1. 收口成功后形成 lead / submission / bind task
2. bind success 后写 CRM
3. CRM write 后必须 verify
4. 只有 verify 成功的 lead 才能进入官方群审批候选池
5. 官方群审批前必须再做一次 CRM eligibility check

### B. 官方群审批 eligibility check
6. 根据 `lead_id` 或 `submission_id` 读取当前 lead
7. 校验当前状态必须处于：`bind_success / group_join_pending / group_join_failed`
8. 校验 lead 已有本次 CRM verify 信号
9. 在 CRM 中重新查找对应用户
10. 核对字段至少包括：
   - `ywId`
   - `mobile`
   - `appName`
   - `deptName`
   - `pendaftaranGroup`
11. 若 CRM 已显示目标官方群已存在（如 `wa == target_group`），则默认判定为重复审批，不再重复放行
12. 但若该申请人当前仍真实存在于官方群待审批列表中（典型场景：误退群后重新申请），且 live CRM 仍命中、并且本地无异常标记，则允许重新自动审批通过
13. 若命中异常标记（如 `manual_review_pending` / `review_status in {pending,retry_requested,rejected}` / `routing_decision=manual_review`），即使 live CRM 命中且申请人仍在待审批列表，也必须拦截并转人工复核
14. eligibility check 的结果必须可审计

### C. 官方群自动审批执行
13. 系统需要一个官方群审批执行器（API / 浏览器自动化 / 其他稳定执行器）
14. 执行前必须先跑 eligibility check
15. 执行结果必须标准化为：
   - success
   - failed
   - retryable_failed
   - manual_required
16. 执行器结果必须写回 `group_join_jobs` / `automation_tasks`
17. 失败原因必须可区分：
   - 已在群中
   - 用户不符合资格
   - 平台拒绝
   - 会话失效
   - 风控 / 验证码
   - 网络/执行失败

### D. CRM 写回与统计
18. 官方群成功后，CRM 用户记录中的官方群字段要更新（当前字段为 `wa`）
19. 写回后必须做 verify
20. 审批动作本身要形成统计记录：
   - 审批通过人数
   - 审批拒绝人数
   - 重复审批跳过人数
   - 待处理人数
21. CRM 中需要有“进群人数/审批结果”统计承载
22. 至少要支持按官方群/注册群/公会/时间维度统计

### E. 审计与运营补救
23. 每次 eligibility check 都应写审计日志
24. 每次官方群审批动作都应写审计日志
25. 失败任务应进入异常池
26. 需要支持 retry / manual override / resubmit 区分
27. 运营应能看到：
   - 哪些人待官方群审批
   - 哪些人被拒
   - 哪些人因 CRM 不匹配被拦截
   - 哪些人因重复审批被跳过

---

## 3. 对照当前代码的差距表

### 3.1 已实现
1. intake / submission / bind / CRM verify 主链已存在
2. `group_join` 已是正式业务节点
3. 已存在：
   - `group_join_jobs`
   - `_queue_group_join_after_verified_crm(...)`
   - `group_join_result(...)`
   - `group_join_success / group_join_failed`
4. 官方群成功后 CRM 用户字段 `wa` 已会更新并 verify
5. 已有：
   - exception queue
   - SLA summary
   - retry-bind / retry-crm / resubmit
   - runtime-health / recent traces
6. 注册群批次人数写入 CRM 已落地：
   - `POST /api/registration-groups/approval-batches`
   - CRM 模块：`ywruquninfo`

### 3.2 部分实现
1. 官方群审批流程已经从“后端状态骨架 + 结果回写”推进到“资格校验 + 审批决策接口”
2. 批次评估已存在：
   - `POST /api/ops/approval-batches/evaluate`
   - `GET /api/ops/approval-batch-queue`
3. 已新增官方群审批前 CRM eligibility gate：
   - `POST /api/official-groups/approval-checks`
4. 已新增官方群审批决策接口：
   - `POST /api/official-groups/approval-decisions`
5. 目前通过依赖注入的 `OFFICIAL_GROUP_APPROVAL_EXECUTOR` 抽象执行真正审批动作
6. 官方群统计目前仍以本地状态与 sync log 为主，没有形成完整 CRM 聚合统计承载

### 3.3 未实现 / 缺口
1. 真实 WhatsApp 官方群自动审批执行器
2. 官方群审批动作级统计与 CRM 聚合承载
3. 官方群审批失败的专门 retry / executor health / manual continue 流程
4. operator console 中面向官方群审批的专门工作面

---

## 4. 本轮已补的缺口代码

### 第一批：CRM eligibility gate
1. `POST /api/official-groups/approval-checks`
2. 新请求模型：`OfficialGroupApprovalCheckRequest`
3. 新服务方法：`official_group_approval_check(...)`
4. 审计事件：`official_group_approval_eligibility_checked`

这一层负责：
- 校验 lead 是否已进入官方群审批阶段
- 校验当前 lead 是否已有 CRM verify 信号
- 回查 CRM 是否存在匹配客户
- 检查 CRM 当前 `wa` 是否已指向目标官方群
- 对“误退群后重新申请”的场景做例外放行：若申请人当前仍在官方群真实待审批列表、live CRM 命中且无异常标记，则允许重新自动审批
- 返回 `eligible / reason_code / next_action / crm_snapshot`

### 第二批：审批决策接口 + 执行器抽象
1. `POST /api/official-groups/approval-decisions`
2. 新请求模型：`OfficialGroupApprovalDecisionRequest`
3. 新服务方法：`official_group_approval_decision(...)`
4. 新内部 helper：`_latest_group_join_task(...)`
5. 新依赖注入点：`OFFICIAL_GROUP_APPROVAL_EXECUTOR`
6. 新审计事件：
   - `official_group_approval_decision_executed`
   - `official_group_approval_decision_skipped`

当前审批决策接口能力：
- 先执行 eligibility check
- 若不符合，则跳过执行器并返回原因
- 若符合，则调用 `OFFICIAL_GROUP_APPROVAL_EXECUTOR.approve(...)`
- 再把执行结果回写进现有 `group_join_result(...)`
- 从而复用：
  - `group_join_jobs`
  - `automation_tasks`
  - CRM `wa` 更新与 verify
  - timeline / sync_logs / audit log

### 第三批：执行器健康与审批统计基础接口
1. `GET /api/ops/official-group-approval-executor-health`
2. `GET /api/ops/official-group-approval-summary`
3. 新服务方法：
   - `official_group_approval_executor_health()`
   - `official_group_approval_summary()`

当前能力：
- executor health：
  - 未配置时返回 `configured=false`
  - 已配置时可透传执行器 `health()` 结果
- approval summary：
  - `pending_count`
  - `approved_count`
  - `failed_count`
  - `skipped_duplicate_count`
  - `retryable_failed_count`
  - `manual_required_count`
  - `by_target_group`
- 当前统计来源：
  - `leads`
  - `group_join_jobs`
  - `operator_audit_log`

### 第四批：真实执行器骨架
1. 新文件：`app/official_group_executor.py`
2. 新类：`WebhookOfficialGroupApprovalExecutor`
3. `create_app(...)` 已支持按配置自动装配 webhook executor：
   - `OFFICIAL_GROUP_APPROVAL_EXECUTOR_KIND=webhook`
   - `OFFICIAL_GROUP_APPROVAL_WEBHOOK_URL`
   - `OFFICIAL_GROUP_APPROVAL_WEBHOOK_TOKEN`
   - `OFFICIAL_GROUP_APPROVAL_WEBHOOK_TIMEOUT_SECONDS`
4. 支持可注入 session，便于测试和后续替换为真实实现

当前骨架能力：
- `health()`
- `approve(...)`
- 向配置的 webhook URL 发起审批请求
- 统一返回：
  - `status`
  - `result_code`
  - `result_reason`
  - `raw_result`
- 当前 schema version：`official-group-webhook-v1`
- 当前已明确的 webhook 上游状态分类：
  - `success`
  - `retryable_failed`
  - `manual_required`
  - 其他状态统一归为 `failed`
- 当前归一化语义：
  - `retryable_failed` -> 本地返回 `status=failed`，并在 `raw_result` 标记：
    - `execution_disposition=retryable_failed`
    - `retryable=true`
  - `manual_required` -> 本地返回 `status=failed`，并在 `raw_result` 标记：
    - `execution_disposition=manual_required`
    - `requires_human_action=true`
- 当前属于真实执行器接线骨架，不等于已接通具体 WhatsApp 官方群平台

### 当前 reason_code
- `eligible`
- `lead_not_ready_for_official_group`
- `crm_verification_missing`
- `crm_adapter_not_configured`
- `crm_customer_not_found`
- `already_in_target_group`

---

## 5. 本轮验证状态
已新增测试并通过：
1. `test_official_group_approval_check_returns_eligible_when_crm_verified_and_target_group_not_yet_joined`
2. `test_official_group_approval_check_rejects_when_crm_already_points_to_target_group`
3. `test_official_group_approval_decision_executes_executor_and_closes_group_join_flow`
4. `test_official_group_approval_decision_skips_executor_when_not_eligible`
5. `test_official_group_approval_executor_health_reports_configured_executor`
6. `test_official_group_approval_summary_counts_pending_approved_and_skipped_duplicates`
7. `test_create_app_can_build_webhook_official_group_executor_from_settings`
8. `test_webhook_official_group_executor_posts_expected_payload`
9. `test_webhook_official_group_executor_normalizes_retryable_failed_response`
10. `test_webhook_official_group_executor_normalizes_manual_required_response`

当前回归结果：
- 定向官方群审批链路：`14 passed, 1 warning`
- 全量：`205 passed, 1 warning`

说明：
- 第一批把“官方群审批前 CRM 资格校验闸口”补出来了
- 第二批把“审批决策接口 + 可注入执行器抽象”补出来了
- 第三批把“执行器健康接口 + 审批统计基础接口”补出来了
- 第四批把“真实 webhook 执行器骨架 + create_app 自动装配”补出来了
- 第五批把“webhook schema version + 错误分类 / retry 语义归一化”补出来了
- 第六批把“decision API 对 retry/manual_required 的 follow-up 语义 + bridge 协议文档”补出来了
- 第七批把“exception queue 与 approval summary 对 retry/manual_required 的联动”补出来了
- 第八批把“retry-official-group-approval 接口与重试闭环”补出来了
- 现在后端已经具备：
  - eligibility gate
  - approval decision API
  - retry-official-group-approval API
  - executor injection point
  - executor health endpoint
  - approval summary endpoint
  - webhook executor skeleton
  - retryable/manual_required normalization
  - decision follow_up_action 语义
  - bridge schema doc v1
  - summary 中的 retryable/manual_required 聚合
  - exception queue 中的 retry/manual latest_action
  - group_join 结果回写复用
- 下一步要做的，就是拿真实 WhatsApp 官方群 bridge 按这个协议接入并跑生产前联调

---

## 6. 下一批建议实现顺序
1. 拿真实 WhatsApp 官方群 bridge 按 `official-group-webhook-v1` 接入
   - 对齐真实 webhook URL
   - 对齐真实 Bearer token
   - 按文档落请求/响应 schema
2. 在真实 bridge 接入后，优先跑端到端生产前联调：
   - approval-check
   - approval-decision
   - retry-official-group-approval
   - exception-queue / summary / crm verify
3. CRM 聚合统计承载接口确认
   - 当前 summary 先基于本地 `leads/group_join_jobs/operator_audit_log`
   - 若 CRM 有对应模块，再把审批统计同步到 CRM
4. operator console 中补官方群审批工作面
