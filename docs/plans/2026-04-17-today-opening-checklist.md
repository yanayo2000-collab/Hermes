# 2026-04-17 今天开工清单

生成时间：2026-04-17 10:36:46 CST

## 0. 当前运行态快照
- 后端 runtime-health：
  - CRM enabled = true
  - CRM base_url = `http://47.236.9.71:8310/enterprise-admin`
  - default_app = `FUMI`
  - default_guild = `Permata`
  - AUTO_BIND_SIMULATION = true
  - success_rate = 1.0
- 当前口径：配置中心 current preset 已能真正驱动运行态，不再被旧 env 默认值覆盖。

## 1. 昨天已经定死的业务硬规则
1. intake bot 必须自动回复，且用户侧回复一律 English only。
2. 前置校验先于 bind / CRM：
   - 必须识别到 `Phone + ID + Group`
   - Phone 必须为 `+<country code> <number>`
   - Linky / FUMI 的 ID 都必须严格 8 位数字
   - Group 必须为 `English-Number`，例如 `Piso-12`
3. `Piso12` 这类值不是 missing group，而是 invalid group format。
4. OCR/screenshot 优先于 text；explicit 优先于 inferred。
5. OCR 中 `Agensi saya / Nama Guild / Agency / Guild` 这类显式标签只映射 agency/dept，不能当 registration group。
6. registration group 只从 text 取，不从 OCR 取。
7. 初次 CRM 写入：
   - `pendaftaranGroup = registration group`
   - `wa = ''`
8. official group (`wa`) 只在后续进群审批/成功阶段更新。
9. 每次 submission 都必须真的调用 CRM create。
10. 只有“本次 CRM create 成功 + query-back 精确回查成功”才能回复 `**✅ Success**`。
11. duplicate / 不可验证 / CRM 拒绝 / 映射失败，都绝不能回复成功。
12. CRM 下拉映射如果 live lookup 失败：
   - 有缓存走缓存
   - 无缓存则 fail-closed，不允许空 `appId/deptId` 下发。

## 2. 昨天已落地完成的能力
### 2.1 intake 解析与校验
- 支持 bare multiline
- 支持顺序打乱
- 支持 text + OCR/image 混合
- phone / ID / Group 靠语义识别，不靠固定行号
- Feishu post 转义文本可自动反转义
- irrelevant chatter 与 missing-fields 已分流

### 2.2 OCR 规则
- OCR 显式标签可提取 account / dept 线索
- OCR guild/agency 标签不再错误写成 registration group
- split-token OCR 有容错

### 2.3 确定性快路径
- intake 可绕过慢 chat-agent 路径走 deterministic fast path
- 已验证文本/CRM路径能落在约 5 秒目标内

### 2.4 CRM 成功判定
- 不再信任 `create_customer(code=0)` 单独成立
- 需要 query-back 精确匹配：
  - `ywId`
  - `mobile`
  - `appName`
  - `deptName`
  - `pendaftaranGroup`

### 2.5 运行态与可观测性
- 已有 `sync_logs`
- 已有 `/api/ops/runtime-health`
- current preset 已能在重启后恢复到 runtime defaults

## 3. 今天开工优先级
### P0：继续盯 live submission 真值
目标：保证所有新的 `✅` 都能在本地与 CRM 两边拿到证据闭环。

每次新 submission 到来后按这个顺序核对：
1. 看 bot 最终回复内容
2. 查最新 `lead`
3. 查最新 `sync_log`
4. 查 CRM query-back 是否 exact-match
5. 只有完全一致，才算真实成功样本

### P0：继续盯 gateway 稳定性是否影响业务
重点不是单看 `ERROR`，而是看：
1. 是否自动 reconnect
2. reconnect 后新消息是否还能打到 `/api/intake/lark/events`
3. reconnect 后是否仍能正常落 lead + sync_log + CRM verified

### P1：继续收集“成功但不可见”与“成功且可见”差异样本
重点对比字段：
- `ywId`
- `mobile`
- `appName`
- `deptName`
- `pendaftaranGroup`
- create response
- query-back response

### P1：继续观察 CRM dropdown/live lookup 稳定性
关注：
- `get_apps` / `get_depts` 是否仍有 502 / 非 JSON
- live lookup 失败时缓存是否稳定兜底
- 是否还出现 app mapping 临时失效导致 retry-once 模板

## 4. 今天不要再回退/破坏的点
1. 不要把配置中心回退成手写输入框。
2. 不要把成功判定回退成只看 `create code=0`。
3. 不要把 OCR guild/agency 再当 registration group。
4. 不要把 duplicate 提交判成成功。
5. 不要把 DB preset 与 runtime defaults 再次脱钩。

## 5. 当前可直接引用的沉淀文档
- `docs/plans/2026-04-16-lark-intake-chain-and-hard-requirements.md`
- `docs/plans/2026-04-16-crm-write-verification-and-coworker-intake-debugging.md`
- `docs/plans/2026-04-17-today-opening-checklist.md`

## 6. 今日判断口径（简版）
- `POST /api/intake/lark/events = 200 OK` 只代表 webhook 收到，不代表业务成功。
- 业务成功的最小证据链：
  1. 有对应 lead
  2. 有对应 sync_log
  3. sync_log 显示本次写入成功
  4. CRM query-back exact-match 成立
