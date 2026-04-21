# MCN AI Automation 数据模型与 API 设计 V1

> 目标：把当前 P0 自动化服务从“基础骨架”推进到“可联调、可扩展、可追踪”的实现层。

## 1. 设计范围

本版补充 3 个核心部分：
- 数据表结构草案
- Lead 状态机触发条件表
- API 设计第一版

本版默认围绕现有业务闭环：
- 广告 → 落地页 / AI 对话
- 用户申请加入 WhatsApp 注册群
- 注册群的人工客服管理员手动审批入群（后续简称“注册客服”）
- 线索入库，并记录用户对应的注册群组来源
- 用户提交账号 ID 或账号截图
- 若拿到纯数字账号 ID，则直接进入公会后台绑定
- 若拿到截图，则先识别账号 ID，识别成功后再进入公会后台绑定
- 绑定成功后写入 CRM，并保留注册群组来源字段
- WhatsApp 官方群后续再由人工审批
- 日报 / 漏斗统计 / 注册客服效率统计

## 2. 数据模型设计

### 2.1 已有表
当前项目已有：
- `leads`
- `customer_projection`
- `lead_events`
- `automation_tasks`
- `sync_logs`

这些表已经能支持：
- lead 入库
- 事件收集
- 自动化任务生成与结果回写
- CRM 投影同步
- 基础同步日志

### 2.2 建议新增表

#### A. lead_status_history
作用：完整记录状态流转，便于追踪“谁在什么时候、因为什么把 lead 推进到下一阶段”。

建议字段：
- `history_id TEXT PRIMARY KEY`
- `lead_id TEXT NOT NULL`
- `from_status TEXT`
- `to_status TEXT NOT NULL`
- `trigger_type TEXT NOT NULL`
- `trigger_source TEXT NOT NULL`
- `trigger_event_id TEXT`
- `trigger_task_id TEXT`
- `operator_id TEXT`
- `operator_name TEXT`
- `remark TEXT`
- `created_at TEXT NOT NULL`

索引建议：
- `INDEX idx_lead_status_history_lead_id_created_at (lead_id, created_at)`
- `INDEX idx_lead_status_history_to_status (to_status)`

#### B. account_submissions
作用：统一记录用户提供的账号信息输入，无论是纯数字账号 ID 还是截图，都先落到这里，再决定是否直接绑定或先识别。

建议字段：
- `submission_id TEXT PRIMARY KEY`
- `lead_id TEXT NOT NULL`
- `task_id TEXT`
- `submission_type TEXT NOT NULL`  (`account_id` / `screenshot`)
- `account_id TEXT`
- `account_id_type TEXT`
- `file_url TEXT`
- `file_type TEXT`
- `source_channel TEXT`
- `submitted_by TEXT`
- `recognition_status TEXT NOT NULL DEFAULT 'not_needed'`
- `recognized_account_id TEXT`
- `recognition_raw TEXT NOT NULL DEFAULT '{}'`
- `submitted_at TEXT NOT NULL`
- `remark TEXT`
- `created_at TEXT NOT NULL`
- `updated_at TEXT NOT NULL`

索引建议：
- `INDEX idx_account_submissions_lead_id (lead_id)`
- `INDEX idx_account_submissions_account_id (account_id)`
- `INDEX idx_account_submissions_recognized_account_id (recognized_account_id)`
- `INDEX idx_account_submissions_recognition_status (recognition_status)`

#### C. bind_check_jobs
作用：记录公会后台手动绑定任务与结果回写。

建议字段：
- `job_id TEXT PRIMARY KEY`
- `lead_id TEXT NOT NULL`
- `submission_id TEXT`
- `account_id TEXT NOT NULL`
- `guild_code TEXT`
- `check_source TEXT NOT NULL`
- `status TEXT NOT NULL`
- `result_code TEXT`
- `result_reason TEXT`
- `raw_result TEXT NOT NULL DEFAULT '{}'`
- `retry_count INTEGER NOT NULL DEFAULT 0`
- `scheduled_at TEXT NOT NULL`
- `finished_at TEXT`
- `created_at TEXT NOT NULL`

索引建议：
- `INDEX idx_bind_check_jobs_lead_id (lead_id)`
- `INDEX idx_bind_check_jobs_status (status)`

#### D. group_join_jobs
作用：记录官方群自动入群任务。

建议字段：
- `job_id TEXT PRIMARY KEY`
- `lead_id TEXT NOT NULL`
- `target_group TEXT NOT NULL`
- `join_type TEXT NOT NULL`
- `status TEXT NOT NULL`
- `result_code TEXT`
- `result_reason TEXT`
- `retry_count INTEGER NOT NULL DEFAULT 0`
- `last_error TEXT`
- `evidence_url TEXT`
- `scheduled_at TEXT NOT NULL`
- `finished_at TEXT`
- `created_at TEXT NOT NULL`

索引建议：
- `INDEX idx_group_join_jobs_lead_id (lead_id)`
- `INDEX idx_group_join_jobs_status (status)`

#### E. reengagement_jobs
作用：记录失败补偿与二次跟进任务。

建议字段：
- `job_id TEXT PRIMARY KEY`
- `lead_id TEXT NOT NULL`
- `job_type TEXT NOT NULL`
- `trigger_reason TEXT NOT NULL`
- `status TEXT NOT NULL`
- `payload TEXT NOT NULL DEFAULT '{}'`
- `scheduled_at TEXT NOT NULL`
- `finished_at TEXT`
- `created_at TEXT NOT NULL`

索引建议：
- `INDEX idx_reengagement_jobs_lead_id (lead_id)`
- `INDEX idx_reengagement_jobs_status (status)`

#### F. registration_group_approval_batches
作用：记录注册客服每次手动审批注册群入群的一批用户，用于统计注册客服效率，并关联不同广告/投手/素材引流到的注册群组。

建议字段：
- `batch_id TEXT PRIMARY KEY`
- `registration_group TEXT NOT NULL`
- `approved_count INTEGER NOT NULL`
- `approved_by TEXT`
- `approved_by_name TEXT`
- `source_platform TEXT`
- `source_campaign TEXT`
- `source_adset TEXT`
- `source_ad TEXT`
- `operator_team TEXT`
- `crm_reported INTEGER NOT NULL DEFAULT 0`
- `remark TEXT`
- `approved_at TEXT NOT NULL`
- `created_at TEXT NOT NULL`

索引建议：
- `INDEX idx_registration_batches_group_approved_at (registration_group, approved_at)`
- `INDEX idx_registration_batches_operator_approved_at (approved_by, approved_at)`

#### G. daily_funnel_snapshot
作用：沉淀日报和 campaign 漏斗结果，避免每次都扫全量明细表；同时支持按注册群组维度还原不同广告测试组和注册客服效率。

建议字段：
- `snapshot_id TEXT PRIMARY KEY`
- `snapshot_date TEXT NOT NULL`
- `source_platform TEXT NOT NULL`
- `source_campaign TEXT`
- `country TEXT NOT NULL`
- `registration_group TEXT`
- `lead_count INTEGER NOT NULL DEFAULT 0`
- `registration_group_approved_count INTEGER NOT NULL DEFAULT 0`
- `engaged_count INTEGER NOT NULL DEFAULT 0`
- `account_submitted_count INTEGER NOT NULL DEFAULT 0`
- `bind_success_count INTEGER NOT NULL DEFAULT 0`
- `group_join_success_count INTEGER NOT NULL DEFAULT 0`
- `cost_amount REAL`
- `currency TEXT`
- `created_at TEXT NOT NULL`
- `UNIQUE(snapshot_date, source_platform, source_campaign, country, registration_group)`

## 3. Lead 状态机 V1

### 3.1 状态定义
- `new`：刚入库，尚未建立有效互动
- `engaged`：已经发生有效互动（如点 WhatsApp、AI 对话、客服接触）
- `account_submitted`：用户已提交账号 ID 或截图
- `recognition_pending`：已收到截图，等待识别账号 ID
- `bind_check_pending`：等待在公会后台执行手动绑定或回写绑定结果
- `bind_success`：绑定核验通过
- `bind_failed`：绑定核验失败
- `group_join_pending`：已准备进入官方群
- `group_join_success`：官方群加入成功
- `group_join_failed`：官方群加入失败
- `re_engage_pending`：等待补偿触达 / 二次转化
- `closed`：流程结束，不再继续跟进

### 3.2 触发条件表

| 当前状态 | 触发器 | 条件 | 下一个状态 |
|---|---|---|---|
| new | `registration_group_join_approved` | 注册客服已人工审批用户进入注册群 | engaged |
| new | `contact_clicked` | 用户点击 WA / Message / 联系按钮 | engaged |
| new | `session_started` | AI 或客服会话建立 | engaged |
| engaged | `account_id_submitted` | 用户提交纯数字账号 ID | account_submitted |
| engaged | `account_screenshot_submitted` | 用户提交账号截图 | recognition_pending |
| recognition_pending | `recognition_success` | 成功识别出账号 ID | account_submitted |
| recognition_pending | `recognition_failed` | 未能识别账号 ID，需要人工补录或补偿 | re_engage_pending |
| account_submitted | `bind_check_job_created` | 创建人工绑定/绑定结果回写任务 | bind_check_pending |
| bind_check_pending | `bind_check_success` | 公会后台核验通过 | bind_success |
| bind_check_pending | `bind_check_failed` | 公会后台核验失败 | bind_failed |
| bind_success | `group_join_job_created` | 创建官方群入群任务 | group_join_pending |
| group_join_pending | `group_join_success` | 自动入群成功 | group_join_success |
| group_join_pending | `group_join_failed` | 自动入群失败 | group_join_failed |
| bind_failed | `reengagement_job_created` | 创建补偿/补触达任务 | re_engage_pending |
| group_join_failed | `reengagement_job_created` | 创建补偿/补触达任务 | re_engage_pending |
| re_engage_pending | `manual_close` | 人工确认关闭 | closed |
| group_join_success | `manual_close` | 已完成目标链路 | closed |

### 3.3 状态机实现建议
- 每次状态变化都写入 `lead_status_history`
- 状态变化必须带 `trigger_type` 和 `trigger_source`
- 不允许“无记录直接改 leads.current_status`
- 所有自动任务都应尽量通过事件驱动，而不是静默修改主表

## 4. API 设计 V1

### 4.1 已有 API
- `POST /api/leads/upsert`
- `POST /api/events/collect`
- `POST /api/tasks/create`
- `POST /api/tasks/{task_id}/result`
- `POST /api/crm/customer-sync`
- `GET /api/reports/daily-summary`

### 4.2 新增 API 建议

#### 0. POST /api/registration-groups/approval-batches
用途：登记注册客服某次审批进入注册群的一批人数，并记录对应注册群组来源，用于 CRM 统计和客服效率分析。

请求示例：
```json
{
  "registration_group": "Piso-1",
  "approved_count": 30,
  "approved_by": "cs_001",
  "approved_by_name": "注册客服A",
  "source_platform": "meta",
  "source_campaign": "indo-test-campaign-01",
  "source_adset": "adset-a",
  "source_ad": "creative-3",
  "approved_at": "2026-04-14T10:30:00Z",
  "remark": "上午第一批审批进群"
}
```

返回示例：
```json
{
  "accepted": true,
  "batch_id": "rgb_xxx",
  "crm_report_status": "pending_or_recorded"
}
```


#### 1. POST /api/account-submissions
用途：统一接收用户提交的账号信息；如果是纯数字账号 ID，直接进入绑定；如果是截图，则先进入识别流程。

请求示例 A：纯数字账号 ID
```json
{
  "lead_id": "lead_xxx",
  "task_id": "task_xxx",
  "submission_type": "account_id",
  "account_id": "45772164",
  "account_id_type": "platform_uid",
  "source_channel": "whatsapp",
  "submitted_by": "customer_service",
  "submitted_at": "2026-04-14T12:00:00Z"
}
```

返回示例 A：
```json
{
  "accepted": true,
  "submission_id": "sub_xxx",
  "normalized_account_id": "45772164",
  "next_action": "queue_bind_check"
}
```

请求示例 B：账号截图
```json
{
  "lead_id": "lead_xxx",
  "task_id": "task_xxx",
  "submission_type": "screenshot",
  "file_url": "https://cdn.example.com/account-shot/abc.png",
  "file_type": "image/png",
  "source_channel": "whatsapp",
  "submitted_by": "customer_service",
  "submitted_at": "2026-04-14T12:03:00Z"
}
```

返回示例 B：
```json
{
  "accepted": true,
  "submission_id": "sub_xxx",
  "next_action": "queue_account_recognition"
}
```

#### 2. POST /api/tasks/{task_id}/recognition-result
用途：截图识别结果回写；成功时生成账号 ID，失败时进入人工补录/补偿。

请求示例：
```json
{
  "status": "success",
  "recognized_account_id": "45772164",
  "result_code": "recognized",
  "result_reason": "numeric account id extracted from screenshot",
  "raw_result": {
    "confidence": 0.95,
    "engine": "ocr_or_cv"
  },
  "finished_at": "2026-04-14T12:05:00Z"
}
```

返回示例：
```json
{
  "task_id": "task_xxx",
  "lead_status": "account_submitted",
  "next_action": "queue_bind_check"
}
```

#### 3. POST /api/tasks/{task_id}/bind-check-result
用途：公会后台手动绑定结果回写。

请求示例：
```json
{
  "status": "success",
  "account_id": "45772164",
  "guild_code": "MCN-11",
  "result_code": "bind_ok",
  "result_reason": "account bound to guild",
  "raw_result": {
    "crm_user_id": "crm_123"
  },
  "finished_at": "2026-04-14T12:08:00Z"
}
```

返回示例：
```json
{
  "task_id": "task_xxx",
  "lead_status": "bind_success",
  "next_action": "queue_group_join"
}
```

#### 4. POST /api/tasks/{task_id}/group-join-result
用途：官方群自动入群结果回写。

请求示例：
```json
{
  "status": "success",
  "result_code": "group_join_ok",
  "result_reason": "user joined official group",
  "evidence_url": "https://cdn.example.com/group-proof/1.png",
  "finished_at": "2026-04-14T12:10:00Z"
}
```

返回示例：
```json
{
  "task_id": "task_xxx",
  "lead_status": "group_join_success",
  "next_action": "close_or_education"
}
```

#### 5. GET /api/leads/{lead_id}/timeline
用途：查看单个 lead 的完整生命周期。

返回结构建议：
```json
{
  "lead": {
    "lead_id": "lead_xxx",
    "current_status": "group_join_pending"
  },
  "status_history": [],
  "events": [],
  "tasks": [],
  "sync_logs": [],
  "evidence_files": []
}
```

#### 6. GET /api/reports/funnel
用途：返回按平台/国家/campaign 聚合后的漏斗。

查询参数建议：
- `date_from`
- `date_to`
- `source_platform`
- `country`
- `source_campaign`

返回示例：
```json
{
  "rows": [
    {
      "source_platform": "meta",
      "source_campaign": "camp-a",
      "country": "Indonesia",
      "lead_count": 1000,
      "engaged_count": 420,
      "account_submitted_count": 150,
      "bind_success_count": 90,
      "group_join_success_count": 75
    }
  ]
}
```

## 5. 与现有代码的映射建议

### 5.1 优先修改文件
- `app/main.py`
  - 补新请求模型
  - 补 schema 初始化 SQL
  - 补 service 层方法
  - 补新 API 路由
- `sql/p0_schema.sql`
  - 与代码内 schema 保持一致
- `tests/test_api.py`
  - 为新增接口补测试

### 5.2 推荐迭代顺序
第一批：
1. `lead_status_history`
2. `account_submissions`
3. `POST /api/account-submissions`

第二批：
4. `POST /api/tasks/{task_id}/recognition-result`
5. `bind_check_jobs`
6. `POST /api/tasks/{task_id}/bind-check-result`
7. `group_join_jobs`
8. `POST /api/tasks/{task_id}/group-join-result`

第三批：
8. `GET /api/leads/{lead_id}/timeline`
9. `GET /api/reports/funnel`
10. `daily_funnel_snapshot`

## 6. GitHub 备份与防错现状

当前已经具备：
- 本地 Git 仓库
- 远程 GitHub 仓库
- 自动备份脚本：`scripts/git_auto_backup.sh`
- Workflow 文件：`.github/workflows/ci.yml`

注意：
- 当前 CI 文件是否真正进入远程仓库，取决于 GitHub token 是否具备 `workflow` scope
- 如果 token 不带该权限，代码仍可备份，但 workflow 文件推送会被拒绝

## 7. 下一步最值得继续实现的内容

建议下一轮直接进入代码实现：
1. 扩 schema，新增 `lead_status_history`
2. 新增 `POST /api/evidence/uploaded`
3. 新增 `POST /api/tasks/{task_id}/ocr-result`
4. 为这 3 项补测试

这样做的价值最大：
- 可以把“账号 ID 直接绑定”与“截图识别后再绑定”两条入口统一接起来
- 仍然保持公会后台手动绑定是当前 MVP 主流程
- 是从 P0 骨架走向可联调 MVP 的最短路径
