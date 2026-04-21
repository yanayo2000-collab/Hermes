# MCN 客服自动化全链路 P1 实施计划

> For Hermes: Use subagent-driven-development skill to implement this plan task-by-task.

目标：在已完成 P0（CRM live 启用 + Success 收紧）的基础上，把 MCN 客服自动化从“入口稳、失败链路清晰”推进到“真实成功样本可闭环、多公会可运营、异常可补救、运营可追踪”。

架构：继续沿用当前共享 backend + 多 Lark intake bot + guild executor registry 的模式，不增加新的主入口，不引入 WhatsApp 自动化。优先补“真实成功闭环、自助补单/续权、运营异常池”。

技术栈：FastAPI + SQLite + Live CRM Adapter + Chrome session bind executor + Hermes/Lark websocket gateway。

---

## 当前基线

已完成：
- Lark 多机器人 intake
- 解析/校验/手机号规范化/邀请码规范化
- async ingress + worker + runtime-health
- bind 失败模板映射
- CRM live enabled
- Success 必须依赖本次 CRM verify 成功
- 全量回归：185 passed

当前主要缺口：
1. 真实成功样本闭环验证
2. 多 guild executor 全面实装
3. auth/session 失效后的人工续权机制
4. submission 级补单/重试工具
5. 运营异常池与 SLA 面板
6. group_join 真执行与补救闭环

---

## Task 1: 固化“真实成功闭环”验收脚本

目标：把真实成功样本验证变成可重复执行的标准流程，而不是人工口头判断。

文件：
- Create: `scripts/verify_real_success_chain.py`
- Modify: `README.md`
- Test: `tests/test_api.py`

步骤：
1. 新建脚本，输入 `mobile / account_id / invite_code / registration_group / bot_app_id`。
2. 脚本依次拉取：
   - latest lead
   - latest automation_task(bind_check)
   - latest sync_log
   - runtime-health recent traces
3. 明确输出 4 段判定：
   - parse ok?
   - bind ok?
   - crm create ok?
   - crm verify ok?
4. 只有四段全部成功，最终输出 `REAL_SUCCESS_CONFIRMED`。
5. README 补“真实成功闭环验收命令”。

验收：
- 能对单个 submission 输出结构化结论
- 不能再靠肉眼翻 DB/日志判断

---

## Task 2: 补“人工续权/会话失效”状态层

目标：当 guild executor 登录态失效时，系统能显式标记并等待人工处理，而不是只在失败 reason 里埋文本。

文件：
- Modify: `app/main.py`
- Modify: `tests/test_api.py`

步骤：
1. 为 bind executor 失败统一补归类：
   - `auth_required`
   - `session_expired`
   - `captcha_required`
   - `manual_continue_required`
2. 在 `automation_tasks` 的 bind 结果里保留标准字段：
   - `requires_human_action`
   - `human_action_type`
3. 在 `/api/ops/runtime-health` 或新的 ops 接口里暴露“待人工续权 guild 列表”。
4. 模板层不把这类错误包装成普通 bind failed，而映射到可理解模板。

验收：
- session 过期时不只是 HTTP 文本错误
- 运营能知道“哪个 guild 需要人工续权”

---

## Task 3: 新建 guild executor 健康接口

目标：把“多 guild 可运营”变成可观察状态，而不是只靠 recent bind traces 猜。

文件：
- Modify: `app/main.py`
- Test: `tests/test_api.py`

步骤：
1. 新增 `GET /api/ops/guild-executors/health`。
2. 每个 guild 输出：
   - `guild_name`
   - `enabled`
   - `browser_profile_key`
   - `proxy_region`
   - `bind_concurrency`
   - `last_bind_started_at`
   - `last_bind_finished_at`
   - `last_bind_status`
   - `last_bind_result_code`
   - `requires_human_action`
3. 前端配置中心后续可直接消费这个接口。

验收：
- 不用翻 traces 就能看 guild 执行器健康状态

---

## Task 4: 做 submission 级补单/重试接口

目标：失败后能补，不用只能“再发一遍消息碰运气”。

文件：
- Modify: `app/main.py`
- Test: `tests/test_api.py`

步骤：
1. 新增接口：
   - `POST /api/ops/submissions/{submission_id}/retry-bind`
   - `POST /api/ops/submissions/{submission_id}/retry-crm`
2. 重试时必须保留语义区分：
   - 技术重试 != 人工重提
3. 技术重试复用原 submission，不新建人工 submission 语义。
4. timeline 中明确记录 retry action。

验收：
- 同一 submission 可技术重试
- 不污染“人工发送=一次 submission”的业务口径

---

## Task 5: 做 submission 纠错重提接口

目标：客服填错 phone/code/group 后，运营能在后台修正并重提，而不是依赖重新聊天录入。

文件：
- Modify: `app/main.py`
- Test: `tests/test_api.py`

步骤：
1. 新增接口：
   - `POST /api/ops/submissions/{submission_id}/resubmit`
2. 接收允许修正的字段：
   - `mobile`
   - `registration_group`
   - `invite_code`
   - `account_id`
3. 这条路径算“人工重提”，应创建新 submission，并保留 `original_submission_id`。
4. duplicate 语义继续按现有规则处理。

验收：
- 技术重试和人工重提有明确边界
- 可追踪原 submission 与重提 submission 的关系

---

## Task 6: 做运营异常池接口

目标：把当前 scattered 的失败状态、operator notifications、待处理任务统一成一个可消费的异常池。

文件：
- Modify: `app/main.py`
- Test: `tests/test_api.py`

步骤：
1. 新增 `GET /api/ops/exception-queue`。
2. 聚合来源：
   - bind_check_failed
   - crm_record_failed
   - requires_human_action bind tasks
   - group_join_failed
   - 超时未闭环 submission
3. 支持基本筛选：
   - guild
   - bot_app_id
   - reason type
   - created_at range
4. 返回最小字段：
   - lead_id
   - submission_id
   - task_id
   - current_status
   - exception_type
   - reason
   - latest_action
   - created_at

验收：
- 运营可直接看待处理异常池
- 不必分别翻 leads/tasks/notifications

---

## Task 7: 做最小 SLA 看板接口

目标：先提供机器可读的运营指标，再决定前端呈现。

文件：
- Modify: `app/main.py`
- Test: `tests/test_api.py`

步骤：
1. 新增 `GET /api/ops/sla-summary`。
2. 输出指标：
   - 今日 submission 数
   - 成功数
   - 失败数
   - 待处理数
   - 超过 5 分钟未闭环数
   - top failure reasons
   - 按 guild / bot 汇总
3. 所有统计基于 submission 语义，不混 lead 语义。

验收：
- 能直接回答“今天处理了多少、卡在哪里、哪类错最多”

---

## Task 8: 梳理 group_join 真执行与补救闭环

目标：把 group_join 从“有状态字段”推进到“明确执行责任和补救路径”。

文件：
- Modify: `docs/plans/2026-04-14-operator-console-mvp.md`
- Create: `docs/plans/2026-04-21-group-join-execution-closure.md`

步骤：
1. 明确 group_join 是：
   - 手工执行
   - 半自动回写
   - 还是后续自动化执行
2. 定义失败补救动作：
   - requeue
   - manual review
   - close
3. 明确 CRM official_group update 的时机和依赖。

验收：
- group_join 不再只是状态壳子
- 运营知道失败后怎么处理

---

## 推荐执行顺序

第一周：
1. Task 1 真实成功闭环验收脚本
2. Task 2 会话失效/人工续权状态层
3. Task 3 guild executor 健康接口
4. Task 4 submission 技术重试

第二周：
5. Task 5 submission 纠错重提
6. Task 6 运营异常池
7. Task 7 SLA 看板
8. Task 8 group_join 执行闭环文档化并落实现状

---

## 验收标准

完成本计划后，系统应满足：
- 能证明一条真实 success 完整闭环
- 能快速识别哪个 guild executor 需要人工处理
- 失败后可技术重试或人工纠错重提
- 运营可直接看异常池和 SLA，而不是翻数据库
- group_join 不再是模糊的“后续再说”环节

---

## 执行备注

- 不新增 WhatsApp 自动化
- 不放宽 Success 红线
- 所有改动必须继续对所有 intake bots 统一生效
- 继续保持 `Phone / ID / Group / Code / App / Agency` 统一口径
- 任何新接口都优先返回紧凑 JSON，避免前端先绑死复杂展示结构
