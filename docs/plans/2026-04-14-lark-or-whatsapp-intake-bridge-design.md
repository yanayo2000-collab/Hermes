# Lark 机器人 / 指定 WhatsApp 群 作为人工客服收口入口的链路设计

> 目标：让人工客服继续在熟悉的聊天环境里工作，但把“用户手机号 + 文本 ID / 截图”稳定收口到一个统一入口，再由系统自动推进识别、公会绑定、CRM 入库与后续统计。

## 1. 结论

这个链路可以做，而且比第一版直接接入智齿或直接打通真实 WhatsApp API 更稳。

建议第一版优先采用：
- 方案 A：收口到一个 Lark 机器人
- 方案 B：收口到一个固定 WhatsApp 工作群，再由人工/桥接程序转发到系统

从可控性、执行效率、日志留存、后续自动化扩展看：
- 第一优先：Lark 机器人收口
- 第二优先：WhatsApp 固定工作群收口

原因：
1. 人工客服仍可继续在 WhatsApp 与真实用户沟通，不改变前台业务习惯
2. 系统只要求人工客服把结构化结果“转发/提交”到固定入口，不强求前台渠道直接自动化
3. 收口入口统一后，可以逐步把后端自动化做深，而不被 WhatsApp 风控卡住
4. 后续若拿到公会后台 API 或稳定 RPA，再把 bind 从人工推进到自动，不影响前台入口设计

---

## 2. 推荐架构

### 2.1 第一版推荐架构

真实用户
-> 落地页 / Meta Message
-> 人工客服在 WhatsApp 接待
-> 人工客服把用户资料发到固定收口入口
-> Hermes 读取收口入口消息
-> 结构化解析
-> 创建/更新 lead
-> account submission
-> 识别截图（如需要）
-> 进入 bind 队列
-> 执行公会后台绑定
-> bind success 后写入 CRM
-> 官方群后续人工审批

### 2.2 收口入口两种实现

#### 方案 A：Lark 机器人收口（推荐）
人工客服把资料统一发给一个 Lark 机器人。

优点：
- Hermes 已有成熟 Lark 接入能力
- 消息读取、结构化解析、日志留存都更稳
- 不直接碰 WhatsApp API 风控
- 易做权限控制
- 易做后续按钮/卡片/任务流

缺点：
- 人工客服需要多一步把信息从 WhatsApp 转发到 Lark

#### 方案 B：固定 WhatsApp 工作群收口
人工客服把资料转发到一个固定 WhatsApp 工作群，再由桥接程序/人工抄送进入系统。

优点：
- 客服习惯最少变化

缺点：
- 第一版不宜直接做自动抓群消息，因为 WhatsApp 风控与接入稳定性差
- 日志、权限、重试、去重都更难做

结论：
- 第一版建议以 Lark 机器人作为收口入口
- 如果客服强烈依赖 WhatsApp 群，则用“WhatsApp 群 -> 人工转发给 Lark 机器人”的方式过渡

---

## 3. 第一版人工客服标准动作

### 3.1 客服继续在 WhatsApp 前台接待用户
客服负责：
- 回答基础问题
- 收集用户手机号（默认来自用户 WhatsApp 账号）
- 收集文本账号 ID
- 或让用户发送账户截图

### 3.2 客服把资料转发到固定收口入口
建议统一格式发给 Lark 机器人：

#### 文本 ID 场景
```text
#注册提交
手机号：+62 81234567890
注册群：Piso-5
应用：Linky
公会：Piso
账号ID：45678901
提交方式：文本
客服：dewi01
备注：用户已确认
```

#### 截图场景
```text
#注册提交
手机号：+62 81234567890
注册群：Piso-5
应用：Linky
公会：Piso
提交方式：截图
客服：dewi01
备注：用户已发账户截图，见本条附件
```

### 3.3 Hermes 解析后自动推进
- 文本 ID：直接写入 `/api/account-submissions`，`submission_type=account_id`
- 截图：写入 `/api/account-submissions`，`submission_type=screenshot`
- 若缺字段：回消息要求客服补齐
- 自动生成 bind_check 任务

---

## 4. 系统自动化分工

### 4.1 Hermes / 收口机器人负责
1. 读取人工客服发来的消息
2. 识别是否是有效“注册提交”格式
3. 提取字段：
   - 手机号
   - 注册群
   - 应用
   - 公会
   - 文本 ID / 是否有截图
   - 客服名
   - 备注
4. 校验字段完整性
5. 调用后端接口：
   - `/api/leads/upsert`
   - `/api/account-submissions`
6. 给客服回执：
   - 已收录
   - 待补字段
   - 已进入绑定队列

### 4.2 后端负责
1. 建立/更新 lead
2. 建立 account submission
3. 截图走 recognition
4. 进入 bind_check 队列
5. bind success 后：
   - 调 CRM create / update
6. 记录 timeline / funnel / daily summary

### 4.3 人工仍负责
1. WhatsApp 前台接待
2. 公会后台绑定（第一版）
3. 官方群审批
4. bind failed 后二次沟通

---

## 5. 为什么这个链路适合第一版

### 5.1 优点
1. 不依赖 WhatsApp 官方 API
2. 不依赖智齿接入和审批流程
3. 不改变客服前台工作方式
4. 自动化重点放在真正有价值的后半段
5. 容易逐步替换人工节点

### 5.2 风险可控
主要风险：
- 客服转发格式不统一
- 漏字段
- 错把别人的截图配到错误手机号

应对：
- 强制统一消息模板
- Hermes 只接受 `#注册提交` 开头的结构化消息
- 缺字段不入库，直接回执要求补齐
- 截图必须和同条消息的手机号 / 注册群一起提交

---

## 6. 第一版必须实现的系统能力

### 6.1 Lark 机器人收口能力
新增能力：
1. 监听固定 Lark 会话 / 话题
2. 识别 `#注册提交` 消息
3. 解析结构化字段
4. 读取附件/图片
5. 调后端接口
6. 回执处理结果

### 6.2 后端接口复用
现有可直接复用：
- `POST /api/leads/upsert`
- `POST /api/account-submissions`
- `POST /api/tasks/{task_id}/recognition-result`
- `POST /api/tasks/{task_id}/bind-check-result`
- `GET /api/leads/{lead_id}/timeline`
- `GET /api/ops/bind-queue`
- `POST /api/registration-groups/approval-batches`

### 6.3 需要补的能力
建议补一个“人工收口专用提交接口”，避免 Hermes 侧自己拼装过多逻辑：

#### 建议新增
`POST /api/intake/manual-cs-submissions`

入参建议：
```json
{
  "mobile": "+62 81234567890",
  "registration_group": "Piso-5",
  "app_name": "Linky",
  "dept_name": "Piso",
  "submission_type": "account_id",
  "account_id": "45678901",
  "file_url": null,
  "submitted_by": "dewi01",
  "source_channel": "manual_cs_lark",
  "remark": "用户已确认",
  "submitted_at": "2026-04-14T18:00:00Z"
}
```

后端内部负责：
1. `lead upsert`
2. `account submission`
3. 返回 lead_id / task_id / next_action

这样 Hermes 侧更轻，前后端职责更清晰。

---

## 7. 第一版推荐实施顺序

### Phase 1：先跑通最小闭环
1. 约定人工客服固定消息模板
2. 用 Lark 机器人收口
3. Hermes 解析文本 ID 场景
4. 后端进入 bind 队列
5. bind success 后入 CRM

### Phase 2：补截图场景
1. Lark 机器人读取截图附件
2. 后端走 screenshot submission
3. 识别 -> bind -> CRM

### Phase 3：收口体验优化
1. 自动回执
2. 缺字段追问
3. 去重与重复提交检测
4. 绑定结果回传给客服

### Phase 4：后续再考虑替换前台入口
1. 智齿
2. WhatsApp 直接接入
3. 公会后台自动绑定

---

## 8. 最终建议

当前最优选择：
- 前台继续人工客服 WhatsApp 接待
- 中间用 Lark 机器人做统一收口
- 后台由 Hermes + mcn-ai-automation 自动推进识别、绑定、CRM

不建议第一版直接做：
- 智齿深度接入
- WhatsApp API 自动抓群消息
- 官方群自动审批

建议第一版重点做成：
“人工客服只负责聊天和转发，系统负责后续一切结构化推进。”

这会让第一版真正可落地，而且风险最低。
