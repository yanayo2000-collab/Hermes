# 2026-04-16 Lark 收口链路与硬要求沉淀

## 1. 当前已经确认的完整链路

当前收口链路按下面顺序执行：

1. 人工客服在 Lark intake bot 发送收口消息
2. intake bot 先做语义识别，不依赖固定行位置
3. intake edge 先做前置快速校验
4. 若校验失败：立即回复人工客服，不进入后续 bind / CRM
5. 若校验通过：进入 `/api/intake/lark/events`
6. backend 将消息转换为 `manual_cs_submissions`
7. 创建 / 更新 lead 与 submission
8. 若启用 `AUTO_BIND_SIMULATION=true`：自动执行 bind 模拟
9. bind 成功后才允许写入真实 CRM
10. CRM 写入成功后继续进入 `group_join` 待处理状态

核心业务规则：
- 公会后台绑定才是真正业务闸口
- CRM 不是前置资格判断系统
- 只有 bind success 之后才允许 CRM create/update

---

## 2. intake bot 必备硬要求

### 2.1 回复要求
- 人工客服发送消息后，机器人必须自动回复
- 无论成功还是失败，都必须有明确结果返回
- 回复必须用固定英文标签：
  - Phone
  - ID
  - Group
  - App
  - Agency

### 2.2 收口识别要求
- 不允许只按行顺序硬编码识别字段
- 必须优先按“语义”识别字段
- 行结构只能作为辅助证据，不是主判断依据

### 2.3 前置快速校验要求
在进入 bind / CRM 之前，必须先做：
1. `Phone + ID + Group` 是否都已识别到
2. Phone 是否满足全球统一格式：
   - `+<country code> <number>`
   - 必须有且只有一个空格
3. 当前 App 若为 `Linky`，则 ID 必须正好 8 位数字

任一失败：
- 立即回复
- 不进入 bind
- 不进入 CRM

---

## 3. 当前收口识别规则

### 3.1 Phone 识别
采用全球格式规则：
- `+<country code> <number>`
- 示例：
  - `+62 81234567890`
  - `+1 8888888888`
  - `+52 777777777`

禁止接受：
- `+6281234567890`
- `081234567890`

### 3.2 ID 识别
- 纯数字串视为 ID candidate
- 当前业务规则：
  - `Linky` => ID 必须正好 8 位数字

### 3.3 Group 识别
当前优先识别以下 registration-group 模式：
- `Piso-<digits>`
- `Permata-<digits>`
- `Sampanye-<digits>`
- `Carote-<digits>`

### 3.4 语义识别优先级
对于自由文本 / 裸三行输入：
- 匹配 `+区号 空格 数字串` => Phone
- 匹配群模式 => Group
- 匹配纯数字 => ID

但这只是候选分类规则，最终仍以整体语义识别为主，不以行顺序为主。

---

## 4. 已沉淀的关键测试结论

### 4.1 应命中 invalid ID，而不是 irrelevant
输入：
```text
+62 126165399
Piso-4
901124
```
预期：
- 识别出 Phone / Group / ID
- 再因为 Linky 规则返回：
  - `invalid_account_id_format`
  - `**🚫 Invalid ID. Linky requires exactly 8 digits.**`

### 4.2 裸三行合法输入应通过
输入：
```text
+62 1261215399
Piso-4
90112111
```
预期：
- accepted = true
- 进入 `queue_bind_check` / bind success 后续链路

### 4.3 乱序语义输入也应通过
输入：
```text
90112111
+62 1261215399
Piso-4
```
预期：
- 仍能识别成功
- 不依赖 line1/line2/line3 固定位置

### 4.4 缺 Group 时，Phone 不能吞掉 ID
输入：
```text
+62 1261215399
90112111
```
预期：
- `missing_required_fields`
- Phone = `+62 1261215399`
- ID = `90112111`
- Group = `-`

### 4.5 手机号缺空格应直接失败
输入：
```text
+621261215399
Piso-4
90112111
```
预期：
- `invalid_phone_format`

---

## 5. CRM 联调结论

### 5.1 已确认的真实问题
曾出现“收口 success 但 CRM 没有入库”，根因是：
- 运行中的 8011 服务缺少 `CRM_BASE_URL`
- 导致 `crm_adapter` 未初始化
- bind success 后没有执行真实 CRM create/update

### 5.2 已确认的修复方式
必须保证 8011 运行进程带齐：
- `CRM_BASE_URL`
- `CRM_USERNAME`
- `CRM_PASSWORD`

验证标准：
1. `/api/ops/intake-bot-presets` 能返回真实 CRM 下拉而不只是 fallback
2. 成功样例触发后，CRM `/customer/ywcustomer/page` 能查到真实记录

### 5.3 已验证的真实入库样例
内部验证样例：
```text
+62 1261998888
Piso-4
90119999
```
已确认：
- CRM 可查到真实记录
- creatorName = Hermes
- appName = Linky
- deptName = Piso
- pendaftaranGroup = Piso-4

---

## 6. 当前还值得继续优化的代码点

### 优化 1：为 CRM 写入补 `sync_logs`
现状：
- bind success 后真实 CRM 已经能 create/update
- 但当前 `sync_logs` 对 CRM create/update 没有稳定落库

建议：
- 在 `bind_check_result()` 的 CRM create/update 分支里，统一写 `sync_logs`
- 至少记录：
  - sync_type
  - target_system=crm
  - status
  - request_snapshot
  - response_snapshot
  - created_at

价值：
- 用户以后不用再靠猜测判断“到底有没有写入 CRM”
- 本地 timeline / 排障可直接看证据

### 优化 2：给 8011 增加启动健康检查输出
现状：
- 8011 启动时很难一眼看出 CRM adapter 是否真的挂上

建议：
- 启动时打印明确状态：
  - CRM adapter enabled/disabled
  - CRM login success/failure
  - 当前默认 app/guild

价值：
- 避免再次出现“服务是活的，但 CRM 实际没接上”的误判

### 优化 3：把 App -> ID 规则做成配置化
现状：
- Linky = 8 位 是硬编码规则

建议：
- 抽成配置表/映射，例如：
  - Linky => `^\d{8}$`
  - FUMI => 其他规则

价值：
- 后续扩 App 时不用继续改逻辑分支

### 优化 4：把 Group 白名单配置化
现状：
- `Piso / Permata / Sampanye / Carote` 在规则里写死

建议：
- 改为配置中心或后端常量配置

价值：
- 新群体系扩展成本更低

### 优化 5：把 intake bot 再往 deterministic 收紧
现状：
- intake profile 仍然是 Hermes chat agent 驱动
- 尽管规则已修，但响应时间仍偏长

建议：
- 继续把 intake bot 从“LLM 驱动型 bot”收紧成“确定性收口执行器”
- 尽量直接调用后端规则，而不是先走一轮聊天推理

价值：
- 响应更快
- 回复更稳定
- 避免 persona/session 漂移

---

## 7. 后续联调标准

以后每次改收口规则，都按下面顺序完成后再通知用户测试：

1. 先加测试
2. 跑定向测试
3. 跑全量回归
4. 重启当前真实 8011 服务
5. 做 live 本地验证
6. 如涉及 intake profile 规则，还要：
   - 更新 `SOUL.md`
   - 必要时清理 `state.db`
   - 重启 intake gateway
7. 只有上述全部通过后，才通知用户去 Lark 实测

---

## 8. 当前结论

截至 2026-04-16：
- 收口机器人语义识别规则已经稳定到可用阶段
- intake bot 旧 irrelevant 模板污染问题已定位并修正
- 真实 CRM 入库已重新打通并验证成功
- 下一步最值得继续做的是：
  1. CRM 写入 `sync_logs`
  2. 启动健康检查
  3. App/Group 规则配置化
  4. intake bot deterministic 化
