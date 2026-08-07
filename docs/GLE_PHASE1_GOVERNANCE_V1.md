# GLE Phase 1 治理合同 v1

状态：`DESIGN_READY / EXECUTION_BLOCKED`

权威实施基线：`FINAL_EXECUTION_PLAN_v1.1`

机器合同：`config/gle_phase1_governance_v1.json`

## 1. 目的与边界

E00 只固定一期范围、版本、Owner、Gate 与 Feature Flag 的治理合同。它不接入 API、worker、scheduler 或 Meta adapter，不产生任何数据库或 Meta 写入，也不证明 Gate 0/1 已完成。

### In Scope

- 一个指定 canary 广告账户、一个指定市场。
- Copy-only 实验；唯一变量为 `primary_text`。
- `OFF → LIVE_SHADOW → BOUNDED_EXECUTION → CLOSED` 的单向权限上限。
- Gate 0–3 状态及可核验 receipt hash。
- schema、evaluator、policy、dataset 四个 canonical version 及内容 hash。
- 全局、账户、动作、调度和不确定写入的 kill switch。
- Gate Owner、业务、技术、数据四方实名签字。

### Out of Scope

- 多账户、多市场、图片/视频/落地页/受众/预算/版位实验。
- 自动扩量、跨账户归因、创意生成器重构及 Gate 0/1 功能实现。
- 创建数据库表、修改现有 API/worker/Meta adapter、接入真实执行链。
- 任何生产读取或写入、Meta 写入、部署、backend 重启。

## 2. Copy-only 黄金路径

唯一允许的实验定义是：

1. 固定同一 canary 账户与市场。
2. 固定素材、受众、预算、版位、优化目标及其余广告参数。
3. 仅替换 `primary_text`，生成 challenger 草稿。
4. Live Shadow 的权限上限仅为：创建 PAUSED canary、激活已批准 canary、生成下一实验草稿。
5. Bounded Execution 仅在 Gate 0、1、2 均有 `PASS + receipt_hash` 后，才可能增加暂停 loser 和创建下一 PAUSED challenger；真实调用仍须后续独立执行合同授权。
6. `CLOSED`、`OFF`、任一前置缺失或 kill switch 均不得扩权。

机器动作白名单：

- `CREATE_CANARY_PAUSED`
- `ACTIVATE_CANARY`
- `GENERATE_NEXT_EXPERIMENT_DRAFT`
- `PAUSE_LOSER`
- `CREATE_NEXT_CHALLENGER_PAUSED`

## 3. 模式与晋级门禁

| 模式 | 必要条件 | 权限上限 |
|---|---|---|
| `OFF` | 无 | 无动作 |
| `LIVE_SHADOW` | `global_enabled=true`；四方实名签字；四版本冻结；单账户单市场；Gate 0、1 为 PASS 且各有 receipt hash | Live Shadow 三项白名单动作 |
| `BOUNDED_EXECUTION` | 上述治理前置；Gate 0、1、2 均为 PASS 且各有 receipt hash | Copy-only 五项白名单动作 |
| `CLOSED` | 人工终止无需 Gate 3 PASS | 无动作，绝不继承或扩大旧权限 |

`GLE_PHASE1_FORCE_OFF` 只接受明确真假值。真值强制关闭；假值不改变合同；非法值按关闭处理。它永远不能把 `global_enabled=false` 提升为 true。

Gate PASS 必须按 `Gate 0 → Gate 1 → Gate 2 → Gate 3` 顺序记录；任一后续 Gate 标记 PASS 时，所有前置 Gate 必须已是 PASS 且各自 receipt hash 有效。`CLOSED` 是独立人工终止态，不受此晋级条件约束。

## 4. Owner 与签字表

当前所有槽位为 `UNASSIGNED`，因此任何非 OFF 权限均被阻塞。签字不得使用团队名、岗位名、TBD 或共享账号。

| 角色 | 实名 Owner | 签字时间 | 签字内容 SHA-256 | 职责 |
|---|---|---|---|---|
| Gate Owner | `UNASSIGNED` | — | — | 最终 Gate 口径与晋级决定 |
| Business Signer | `UNASSIGNED` | — | — | 商业边界、canary 账户/市场、动作批准 |
| Technical Signer | `UNASSIGNED` | — | — | 技术实现、发布及回滚边界 |
| Data Signer | `UNASSIGNED` | — | — | canonical dataset、指标与证据完整性 |

签字操作要求：先确定签字对象的 canonical JSON，再记录带时区的 ISO-8601 时间和该对象的 64 位小写 SHA-256；禁止仅填姓名后晋级。

## 5. 当前需要业务确认的参数

- 唯一 canary 广告账户 ID。
- 唯一 canary 市场代码。
- Gate Owner、Business Signer、Technical Signer、Data Signer 的实名。
- schema/evaluator/policy/dataset 的版本号和内容 SHA-256。
- 各 Gate 的独立验收 receipt SHA-256；不得以状态文字代替 receipt。
- 获准动作子集。默认空集，不因模式晋级自动填充。

## 6. 风险登记

| ID | 风险 | 默认控制 | 解除条件 |
|---|---|---|---|
| R-001 | 未实名或代签导致责任不清 | fail closed | 四方实名、时间与签字 hash 完整 |
| R-002 | 版本漂移导致证据不可复现 | 四版本 `UNFROZEN` 时关闭 | 每个版本具名且绑定内容 hash |
| R-003 | Gate 状态被手工改为 PASS | PASS 必须有 receipt hash | 独立验收产物可按 hash 读取 |
| R-004 | 多账户/多市场扩大爆炸半径 | schema 直接拒绝 | 一期不解除；走偏离申请 |
| R-005 | 非 Copy-only 动作混入 | 严格动作和变量白名单 | 一期不解除；走新基线评审 |
| R-006 | 环境变量意外扩权 | force-off 单向收窄 | 不允许解除 |
| R-007 | 本合同被误认为已接入执行链 | 本 PR 不集成 | 后续 Epic 独立实现、测试和验收 |

## 7. 决策日志

| 日期 | 决策 | 理由 | 状态 |
|---|---|---|---|
| 2026-08-06 | 一期只允许单账户、单市场、Copy-only | 控制变量并限制生产风险 | 已固定 |
| 2026-08-06 | 默认全局 OFF、空 canary、空动作集、kill switches 全开 | 在 Owner/版本/Gate 证据缺失时 fail closed | 已固定 |
| 2026-08-06 | `CLOSED` 不继承历史权限 | 关闭态不得成为旁路 | 已固定 |
| 2026-08-06 | E00 不接入任何真实执行链 | 保持治理合同可独立审查和回滚 | 已固定 |

## 8. 偏离申请模板

```text
Deviation ID:
申请人 / 实名 Owner:
申请时间（含时区）:
对应基线与 contract hash:
拟偏离的字段 / 原值 / 新值:
业务理由与预期收益:
影响账户与市场（必须精确列举）:
新增 Meta / 数据写入动作:
风险与最坏结果:
监控、停止条件与 kill switch:
测试与独立 read-back 证据:
回滚步骤、preimage hash 与负责人:
Gate Owner 签字 hash:
Business / Technical / Data 签字 hash:
批准有效期:
```

任何偏离在四方实名签字、证据 hash 和回滚入口齐全前均视为拒绝；不得直接编辑合同绕过申请。

## 9. 验收与回滚

E00 验收仅证明：默认合同严格可读、canonical hash 确定、非法结构被拒绝、权限计算只收窄、模式门禁满足基线。它不证明任何广告对象已创建或可投放。

回滚为移除本 PR 的四个新增文件；因为没有执行链接入、建表或生产写入，不产生数据回滚和 Meta 回滚。
