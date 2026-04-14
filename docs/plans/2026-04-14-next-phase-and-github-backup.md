# MCN AI Automation 下一阶段实施与 GitHub 自动备份方案

> 目标：在现有 P0 自动化服务基础上，继续推进到“可联调、可回传、可备份、可回滚”。

## 1. 当前代码基线

当前本地项目：`/Users/chauncey/work/mcn-ai-automation`

已确认：
- 当前为 Git 仓库
- 当前分支：`main`
- 当前无 Git remote
- 当前无 GitHub CLI (`gh`) 
- 当前未配置全局 git 用户名 / 邮箱 / credential helper
- 当前测试通过：`5 passed`

已实现模块：
- Lead 入库：`/api/leads/upsert`
- Event 收集：`/api/events/collect`
- Task 创建与回写：`/api/tasks/create`、`/api/tasks/{task_id}/result`
- CRM customer sync：`/api/crm/customer-sync`
- Daily summary：`/api/reports/daily-summary`

当前数据库对象：
- `leads`
- `customer_projection`
- `lead_events`
- `automation_tasks`
- `sync_logs`

## 2. 下一阶段开发重点

### 2.1 数据模型继续补强
下一步建议新增或扩展以下对象：

1. `lead_status_history`
- 记录每次状态变化
- 字段建议：
  - `history_id`
  - `lead_id`
  - `from_status`
  - `to_status`
  - `trigger_type`
  - `trigger_source`
  - `trigger_event_id`
  - `operator_id`
  - `created_at`

2. `evidence_files`
- 记录用户上传截图、OCR结果、人工复核结果
- 字段建议：
  - `evidence_id`
  - `lead_id`
  - `file_url`
  - `file_type`
  - `ocr_text`
  - `ocr_status`
  - `review_status`
  - `review_reason`
  - `created_at`
  - `updated_at`

3. `group_join_jobs`
- 记录官方群自动入群任务
- 字段建议：
  - `job_id`
  - `lead_id`
  - `target_group`
  - `join_type`
  - `status`
  - `retry_count`
  - `last_error`
  - `scheduled_at`
  - `finished_at`

4. `daily_funnel_snapshot`
- 用于日报和广告回流分析
- 字段建议：
  - `snapshot_date`
  - `source_platform`
  - `source_campaign`
  - `country`
  - `lead_count`
  - `engaged_count`
  - `account_submitted_count`
  - `bind_success_count`
  - `group_join_success_count`
  - `cost`
  - `created_at`

### 2.2 Lead 状态机建议
建议先统一这些状态：
- `new`
- `engaged`
- `account_submitted`
- `bind_check_pending`
- `bind_success`
- `bind_failed`
- `group_join_pending`
- `group_join_success`
- `group_join_failed`
- `re_engage_pending`
- `closed`

关键状态流转：
- `new -> engaged`
- `engaged -> account_submitted`
- `account_submitted -> bind_check_pending`
- `bind_check_pending -> bind_success | bind_failed`
- `bind_success -> group_join_pending`
- `group_join_pending -> group_join_success | group_join_failed`
- `bind_failed | group_join_failed -> re_engage_pending`

### 2.3 API 继续补充
建议优先新增这些接口：

1. `POST /api/evidence/uploaded`
- 接收截图回传
- 写入 evidence 记录
- 触发 OCR 审核任务

2. `POST /api/tasks/{task_id}/ocr-result`
- OCR 任务写回
- 输出识别状态、命中字段、公会信息

3. `POST /api/tasks/{task_id}/bind-check-result`
- 公会绑定核验结果回写
- 若成功，自动生成入群任务

4. `POST /api/tasks/{task_id}/group-join-result`
- 官方群自动入群结果回写
- 若失败，进入补偿队列

5. `GET /api/leads/{lead_id}/timeline`
- 返回 lead 全生命周期轨迹
- 用于运营排查和客服追踪

6. `GET /api/reports/funnel`
- 按国家 / 广告平台 / campaign 输出转化漏斗

## 3. GitHub 自动备份目标

目标不是单纯“有个 Git 仓库”，而是形成下面这套最小闭环：

1. 本地代码随时可提交
2. 自动推送到 GitHub 远程仓库
3. 每次 push 自动跑测试
4. 出错时能快速定位哪次提交引入了问题

## 4. 当前 GitHub 自动备份卡点

当前机器上已确认：
- 项目是本地 Git 仓库，但没有 `origin`
- 没装 `gh`
- 没有全局 git identity
- 没看到 GitHub 凭证配置

所以现在“不能自动备份到 GitHub”的根因非常明确：
- 还没有远程仓库
- 还没有认证

## 5. 建议的最小 GitHub 备份方案

### 方案 A：最稳的最小方案
1. 创建 GitHub 私有仓库，例如：`mcn-ai-automation`
2. 配置本地 remote `origin`
3. 配置 git 用户名 / 邮箱
4. 使用 PAT 或 SSH 完成认证
5. 本地通过脚本自动执行：
   - `git add -A`
   - `git commit`
   - `git push origin main`
6. GitHub Actions 在 push 时自动跑 `pytest`

### 方案 B：加强版
在方案 A 基础上加：
- 定时自动备份
- 失败告警
- branch protection
- release tag

## 6. 已为仓库准备的配套文件

本次会新增：
- `scripts/git_auto_backup.sh`
- `.github/workflows/ci.yml`

作用：
- `git_auto_backup.sh`：完成自动 add / commit / push
- `ci.yml`：每次 push / PR 自动跑测试，降低代码出错风险

## 7. 要真正打通 GitHub 自动备份，还差这 4 个信息

你后面只需要补我这 4 个中的前 3 个即可：

1. GitHub 仓库地址
- 例如：`https://github.com/<owner>/mcn-ai-automation.git`

2. Git 认证方式
- PAT token
- 或 SSH key

3. Git 提交身份
- `git user.name`
- `git user.email`

4. 是否要做“自动定时备份”
- 例如每 30 分钟 / 每 2 小时 / 每次代码变更后手动一键备份

## 8. 推荐立即执行顺序

### 第一步
把 GitHub 仓库建出来，先拿到仓库 URL

### 第二步
配置本地 remote + auth

### 第三步
执行首个 push，把当前 P0 服务备份上去

### 第四步
让 GitHub Actions 自动跑测试

### 第五步
再继续补：
- 数据表结构扩展
- API 请求/响应示例
- CRM / 公会后台 / 官方群接入联调
