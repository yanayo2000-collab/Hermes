# Group Join 执行与补救闭环

目标：把 group_join 从“有状态字段”推进到“明确执行责任、失败补救路径、与 CRM official_group 更新时机一致”的可运营流程。

## 1. 当前现状

系统当前已经具备：
- `group_join` automation task
- `group_join_jobs` 记录
- `POST /api/tasks/{task_id}/group-join-result`
- group_join 成功后尝试 CRM official group update + verify

当前缺口不在数据模型，而在执行责任与运营补救：
- 谁来执行 group join
- 失败后谁处理
- 是否允许重试
- 哪些失败属于技术重试，哪些属于人工处理

## 2. 执行责任定义

### v1 正式规则
- `bind_success + CRM verify success` 后，系统创建 `group_join` task
- v1 默认将 group_join 视为“人工/半人工执行后回写”环节
- 不在这一阶段引入 WhatsApp 自动化
- 系统职责是：
  1. 生成待处理 group_join task
  2. 记录成功/失败
  3. 在成功后回写 CRM official_group 并做 verify
  4. 在失败后把任务暴露进异常池/运营面板

## 3. 成功路径

1. submission 完成 bind success
2. CRM create + query-back verify success
3. 系统创建 `group_join` task
4. 运营或群管理员执行官方群入群动作
5. 调用 `group_join_result(success)` 回写
6. 系统尝试 CRM official_group update
7. CRM official_group query-back verify success
8. lead 进入 `group_join_success` / 后续 close_or_education

## 4. 失败路径分类

### 4.1 技术失败
适合后续做技术重试：
- 回写接口抖动
- CRM official_group update 失败
- 短时网络错误

建议动作：
- 保持 `group_join_failed`
- 进入异常池
- 后续可补 `retry-group-join` 或 `retry-group-crm` 接口

### 4.2 业务失败
不应机械重试：
- 用户未通过官方群审批
- 群已满
- 目标群错误
- 用户资料不符合群规则

建议动作：
- 记录明确失败 reason
- 进入异常池
- 运营人工决定：
  - 再次沟通
  - 换群
  - close

## 5. 当前推荐补救动作

在本项目当前阶段，group_join 失败后的最小闭环建议是：
- 先统一进入 `exception_queue`
- `latest_action` 标记为 `retry_group_join`
- 运营通过 Lead timeline + 群管理动作判断是否重做

在还未新增专门 `retry-group-join` 接口前：
- 可以先通过 timeline / task 视图人工回查
- 等下一阶段再补独立 retry endpoint

## 6. 与 CRM 的关系

关键规则：
- CRM customer create/verify 成功后，才允许创建 group_join task
- group_join 成功后，才允许写 CRM official_group
- CRM official_group update 也必须 query-back verify
- 不要在 group_join 失败时把 CRM 标成官方群成功

## 7. 运营建议动作

### group_join pending
- 群管理员执行审批/拉群
- 完成后尽快回写结果

### group_join failed
- 打开异常池
- 看失败 reason
- 判断属于：
  - 技术重试
  - 人工再次处理
  - 关闭

### group_join success
- 核对 CRM official_group verify 是否成功
- 若 CRM official_group verify 失败，则仍按异常处理，不算完整闭环

## 8. 后续建议接口

后续如果继续增强，建议新增：
- `POST /api/ops/group-join/{task_id}/retry`
- `POST /api/ops/group-join/{task_id}/retry-crm`
- `POST /api/ops/group-join/{task_id}/close`

但当前阶段先不急着做，优先把 submission 重提、异常池、SLA 跑稳。

## 9. 当前结论

- group_join 在 v1 中继续保持“人工执行 + 系统回写/追踪”
- 不引入 WhatsApp 自动化
- 成功闭环定义仍然严格：
  - bind success
  - CRM create verify success
  - group_join success
  - CRM official_group verify success（如启用官方群回写场景）
- 失败必须进入异常池，而不是沉没在 timeline 里
