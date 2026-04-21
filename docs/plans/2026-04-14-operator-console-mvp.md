# 运营操作台 MVP 设计与接口清单

> 目标：为当前 MCN AI 自动化 MVP 提供一个可试运行的运营操作台，让人工绑定、公会确认、官方群审批回写、CRM 入库与异常补偿可以稳定落地。

## 1. 为什么现在先做操作台

当前系统已经具备：
- 账号提交双入口（文本 ID / 截图）
- 识别结果回写
- bind 结果回写
- group join 结果回写
- CRM 真实登录/查询/新增/更新/图片上传
- timeline / funnel / daily summary

但真实业务中仍有几个关键人工节点：
- 注册客服手动审批用户进入 WhatsApp 注册群
- 公会后台手动绑定
- 绑定失败后的二次沟通
- WhatsApp 官方群人工审批

因此，当前最有价值的不是继续堆底层接口，而是做一个最小运营操作台，把“人工动作”变成：
- 有列表
- 有状态
- 可回写
- 可追踪
- 可统计

## 2. MVP 范围定义

### 本期必须做
1. 注册群审批批次登记列表 / 表单
2. 待绑定任务列表
3. 待入群任务列表
4. Lead 详情页（timeline）
5. 漏斗看板基础页
6. 最小操作按钮：
   - 绑定成功
   - 绑定失败
   - 入群成功
   - 入群失败

补充说明：
- 凭证图片上传不是主流程必需项
- 只要公会后台绑定成功，就应进入 CRM 入库
- 凭证上传如果保留，也仅作为可选补充能力，不应阻塞 CRM 入库或入群流转

### 本期不做
- 完整权限系统
- 复杂角色管理
- 批量自动审批
- 大型报表中心
- 多语言 UI

## 3. 操作台信息架构

### 页面 0：注册群审批批次登记
用途：
- 给注册客服登记某次人工审批进入注册群的一批用户数量
- 让系统后续按注册群组维度统计客服效率、广告测试组效果、投手素材效果

建议展示/录入字段：
- registration_group
- approved_count
- approved_by / approved_by_name
- source_platform
- source_campaign
- source_adset
- source_ad
- approved_at
- remark

建议操作：
- 新增审批批次
- 查看历史批次
- 按注册群组筛选
- 按注册客服筛选

### 页面 1：待绑定列表
用途：
- 给运营查看所有已拿到账号、但还没完成公会绑定的用户

建议展示字段：
- lead_id
- 手机号
- 区号
- ywId / account_id
- 提交方式（文本 / 截图）
- 识别状态
- 所属应用
- 目标公会
- 注册群组
- 当前状态
- 创建时间
- 最近更新时间

建议筛选项：
- 应用
- 国家
- 公会
- 识别状态
- 创建时间

建议操作：
- 查看详情
- 绑定成功
- 绑定失败
- 备注失败原因

### 页面 2：待入群列表
用途：
- 给群管理员/运营查看已绑定成功、等待官方群审批或拉群的用户

建议展示字段：
- lead_id
- 手机号
- ywId
- 所属应用
- 所属公会
- CRM 状态
- 当前官方群状态
- 创建时间

建议操作：
- 入群成功
- 入群失败
- 填写失败原因
- 查看详情

### 页面 3：Lead 详情页
用途：
- 给客服/运营看单个用户的完整生命周期

建议模块：
1. 基础资料
- 手机号
- 区号
- ywId
- 应用
- 公会
- 注册群组

2. 输入资料
- 文本 ID
- 截图信息
- 识别结果

3. 状态历史
- lead_status_history

4. 任务历史
- account recognition
- bind_check
- group_join
- crm_sync

5. CRM 信息
- 是否已入 CRM
- CRM customer id
- fileUrl
- pzStatus

建议操作：
- 重新发起 CRM 同步
- 重新发起 group join 结果回写
- 可选手动触发凭证上传（非必需）

### 页面 4：漏斗看板
用途：
- 给运营快速看不同来源/应用/国家的转化情况

建议维度：
- source_platform
- source_campaign
- country
- registration_group
- appName
- deptName
- approved_by_name（注册客服）

建议指标：
- lead_count
- registration_group_approved_count
- engaged_count
- account_submitted_count
- bind_success_count
- group_join_success_count
- CRM 已入库数
- 注册客服审批效率
- 不同注册群组转化率

## 4. 推荐操作流

### 4.0 注册群审批登记流
1. 注册客服在 WhatsApp 注册群手动审批一批用户进群
2. 在系统登记：注册群组、审批人数、审批时间、注册客服、关联广告来源
3. 系统将该批次写入内部统计记录，并同步/映射到 CRM 统计口径
4. 后续该注册群内成功注册的主播，在个人 CRM 记录中继续带上同一个注册群组来源

### 4.1 绑定操作流
1. 运营打开“待绑定列表”
2. 找到用户
3. 在公会后台执行：
   - 添加主播
   - 输入用户 ID
   - 选择隶属公会
   - 点击确定
4. 根据 toast 结果回写：
   - 成功 -> 绑定成功
   - 失败 -> 绑定失败
5. 如果失败：
   - 填失败原因
   - 系统将用户标记为 re-engage / 需再次沟通

### 4.2 入群操作流
1. 运营打开“待入群列表”
2. 确认该用户已绑定成功 + 已入 CRM
3. WhatsApp 管理员审批进群
4. 回到系统点击：
   - 入群成功
   - 或入群失败

### 4.3 可选凭证上传流
1. 运营在详情页查看该用户是否有截图路径
2. 如果有截图本地路径或可访问路径，可按需补传
3. 点击“上传凭证”
4. 系统执行：
   - CRM 查客户
   - OSS 上传图片
   - CRM attach fileUrl + pzStatus
5. 页面显示：已上传

注意：
- 该流程不是 CRM 入库前置条件
- 主流程仍然是“绑定成功 -> 入库 CRM”

## 5. 后端接口清单（操作台所需）

### 已有可复用接口
- `POST /api/leads/upsert`
- `POST /api/events/collect`
- `POST /api/account-submissions`
- `POST /api/tasks/{task_id}/recognition-result`
- `POST /api/tasks/{task_id}/bind-check-result`
- `POST /api/tasks/{task_id}/group-join-result`
- `GET /api/leads/{lead_id}/timeline`
- `GET /api/reports/funnel`
- `GET /api/reports/daily-summary`
- `POST /api/leads/{lead_id}/voucher-attach`

### 建议新增列表接口

#### A. GET /api/ops/bind-queue
返回所有待绑定 lead

建议过滤参数：
- `app_name`
- `dept_name`
- `country`
- `submission_type`
- `recognition_status`
- `status`

建议只返回状态属于：
- `account_submitted`
- `recognition_pending`
- `bind_check_pending`
- `bind_failed`

#### B. GET /api/ops/group-queue
返回所有待入群 lead

建议只返回状态属于：
- `bind_success`
- `group_join_pending`
- `group_join_failed`

#### C. GET /api/ops/dashboard/summary
返回首页看板汇总数据

建议指标：
- 今日注册群审批批次数
- 今日注册群审批人数
- 今日待绑定数
- 今日绑定成功数
- 今日绑定失败数
- 今日待入群数
- 今日入群成功数
- 今日 CRM 入库成功数
- 今日按注册群组拆分的转化

#### D. GET /api/registration-groups/approval-batches
返回注册客服登记的注册群审批批次列表

## 6. 前端最小组件建议

### BindQueueTable
列：
- lead_id
- mobile
- ywId
- appName
- deptName
- pendaftaranGroup
- status
- recognition_status
- actions

### GroupQueueTable
列：
- lead_id
- mobile
- ywId
- appName
- deptName
- status
- CRM 状态
- actions

### LeadTimelinePanel
模块：
- 基础资料
- account_submissions
- status_history
- tasks
- crm 状态

### FunnelSummaryPanel
卡片：
- 今日 leads
- bind success
- group success
- CRM uploaded
- voucher uploaded

## 7. 最小前后端分工建议

### 后端先做
1. `GET /api/ops/bind-queue`
2. `GET /api/ops/group-queue`
3. `GET /api/ops/dashboard/summary`
4. 保持已有动作接口可直接被按钮复用

### 前端再做
1. 一个首页
2. 一个待绑定页
3. 一个待入群页
4. 一个详情抽屉/详情页

## 8. 推荐开发顺序

### 第一步（最值钱）
实现：
- `GET /api/ops/bind-queue`
- `GET /api/ops/group-queue`

原因：
- 运营先能看任务，就能开始试运行

### 第二步
实现：
- `GET /api/ops/dashboard/summary`
- 详情页复用 timeline

### 第三步
接一个最小前端页面
- 不要求精美
- 先能列表 + 操作即可

## 9. 验收标准

### 操作台 MVP 成功标准
- 运营能看到待绑定列表
- 运营能看到待入群列表
- 运营能点击回写成功/失败
- 运营能查看单个用户 timeline
- 看板能展示基本漏斗数据

## 10. 我对下一步的建议

如果继续推进，我建议直接进入后端第一步实现：
1. `GET /api/ops/bind-queue`
2. `GET /api/ops/group-queue`
3. `GET /api/ops/dashboard/summary`

这样你马上就能有一个最小“运营可看”的操作层。