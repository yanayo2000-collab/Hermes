# GLE Gate 0 / G0-01 精确 ID 归因审计

状态：`READ_ONLY / QUALIFICATION_AND_PROVENANCE_BLOCKED`

基线：`FINAL_EXECUTION_PLAN_v1.1`

## 1. 目标

本工具只回答一个问题：指定账户、市场、实验和时间窗内的 Tugao 成功 Bind 事件，能否通过精确 Meta ID 唯一落到 `ad_experiment + study cell`，再通过原始结构化 `lead_id` 或 `customer_id` 唯一到达现有 CRM truth。

它输出覆盖率、missing/ambiguous reason buckets、版本和确定性证据 hash。它不修复身份，不判断用户是否属于 qualified join，也不生成 Gate Receipt。

## 2. 权威来源与精确链路

```text
ad_dashboard_fact_rows
  account_id + campaign_id + adset_id + ad_id
    → ad_experiment
      source_* IDs + control_definition_json.meta_randomization.study_id/study_cell_id
        → tugao_bind_success_raw_events
          campaign_id + adset_id + ad_id
            → raw_payload_json 的顶层 lead_id / customer_id
              → leads.lead_id / customer_projection.customer_id
```

允许的 canonical identity 只有：

- 原始 JSON 顶层、精确拼写、非空字符串 `lead_id`。
- 原始 JSON 顶层、精确拼写、非空字符串 `customer_id`。

Canonical ID 按原始字节语义比较：禁止 trim、casefold、截断、补前缀或任何其他“修复”。任一 canonical ID 含首尾空白或不是字符串，直接记为 `CANONICAL_IDENTITY_INVALID`。

以下字段即使值与 CRM ID 相同，也不得替代 canonical identity：

```text
customer_user_id
user_key
bind_id
event_id
mobile / phone / WhatsApp
name / real_name
最近时间、相似度或其他推断结果
```

无论输入提供 `lead_id`、`customer_id` 或两者，都必须派生完整的 lead/customer pair，并同时满足：

```text
leads.lead_id == customer_projection.lead_id
leads.matched_customer_id == customer_projection.customer_id
```

任一行或关联键缺失为 `CANONICAL_IDENTITY_NOT_IN_CRM`，任一方向不一致为 `LEAD_CUSTOMER_LINK_CONFLICT`，任一反向一对多为 `AMBIGUOUS_CANONICAL_IDENTITY`。

CRM 的 `lead_id/matched_customer_id/customer_id`、Meta 的 Campaign/Ad Set/Ad ID，以及 Study/Cell ID 同样必须非空且满足 `raw == raw.strip()`；工具不得通过双方同时 strip 隐藏持久化差异。

同一个闭合 pair 无论从 lead-only、customer-only 或双键进入，都统一 canonicalize 为同一个 `CUSTOMER_ID` 去重键。

## 3. CLI 合同

CLI 只能读取一份已 checkpoint、无活动 WAL/rollback journal 的 SQLite snapshot：

```bash
python3 scripts/audit_gle_exact_id_attribution.py \
  --db-path /path/to/checkpointed-snapshot.sqlite3 \
  --expected-db-sha256 <64位小写SHA-256> \
  --account-id <精确广告账户ID> \
  --market MX \
  --experiment-id <experiment-id> \
  --window-start 2026-08-01T00:00:00Z \
  --window-end 2026-08-06T00:00:00Z \
  --project TUGAO \
  --max-events 10000
```

`--experiment-id` 可以重复，但去重后最多 32 个。所有指定实验必须同时满足同一 `account_id + market`、共享一个非空 `study_id`，并具有各自非空且互不重复的 `study_cell_id`。`max_events` 范围为 1–100000；所有事实、Bind 和派生 CRM identity 查询均受输入范围及 `max_events` 约束。读取连接固定为：

```text
SQLite URI mode=ro&immutable=1
PRAGMA query_only=ON
PRAGMA busy_timeout=5000
```

Bind 表本身没有 account_id，因此它不做全表或国家级模糊筛选，而是按每个 experiment 的精确 `(campaign_id, adset_id, ad_id)` tuple 做 OR；不得把三个维度拆成独立集合匹配。CRM 查询也只接受该有界结果中出现的 canonical ID。

Identity scope 通过单个 JSON 数组参数和 SQLite `json_each(?)` 连接到 CRM truth；`max_events` 仍按全局 `+1` fail closed，但不会为每个 canonical ID 分配一个 host parameter。测试在 128 个变量的模拟上限下覆盖 1001 个 identity key。

仅有 `business_date` 而没有 `occurred_at_utc/updated_at_utc` 的事件，只能在输入边界是完整 UTC 日时参与精确命中。部分 UTC 日窗口会保留该事件作为 denominator 证据，但以 `EVENT_TIME_PRECISION_INSUFFICIENT` 阻塞，不计入 exact-meta。

## 4. 输出与隐私

stdout 只输出单行 canonical JSON，键排序固定。不得输出：

- 原始 Bind/Fact/CRM 行。
- `raw_payload_json`。
- lead/customer ID 明文。
- 手机号、姓名或其他 PII。

输出仅包含：

```text
schema_version
status + blocking_reasons
input_contract_hash
source_snapshot_sha256
source_schema_hash
attribution / dedupe / qualification versions
candidate / exact-meta / exact-identity / deduped counts
exact_meta / exact_identity coverage
reason_counts + missing_reason_counts + ambiguous_reason_counts
CRM verification latency p50 / p90 / p95 / max
row_evidence_hash
report_hash
```

`row_evidence_hash` 由排序后的不可逆 event/identity hashes 和 reason codes 计算；明细本身不进入报告。`report_hash` 对除自身外的完整报告做 canonical SHA-256。报告不含当前时间，因此相同 snapshot、schema、输入和代码版本产生相同结果。

这里的 latency 仅指 Bind 事件到 `leads.crm_verified_at` 的 CRM verification latency，不是 qualified-join latency，也不构成 qualification 证据。

数据库 SHA-256、size 和 mtime 在连接前后复核；`-wal` / `-journal` / `-shm` sidecar 也在连接前后做 exists/size/mtime/hash 复核。非空 sidecar、输入 hash 不一致或执行期间任何 snapshot/sidecar 漂移均 fail closed。

## 5. 状态与退出码

当前 qualification rule 尚未冻结，固定输出：

```text
versions.qualification_rule = UNFROZEN
blocking_reasons includes QUALIFICATION_RULE_UNFROZEN
blocking_reasons includes READBACK_PROVENANCE_UNAUDITED
status = BLOCKED
exit code = 2
```

工具绝不根据 `user_quality/current_status/payment_status` 猜测业务规则。

`readback_verified=true` 只是 `ad_experiment` 中的存储态，不是 Plan / Approval / Verification / Receipt 的独立对账证据。G0-01 不扩表读取这些对象，因此固定保留 `READBACK_PROVENANCE_UNAUDITED`；只能由后续 G0-04/G0-05 的独立审计解除。

| Exit | 含义 |
|---|---|
| `0` | 工具级审计完整且无阻塞；只代表审计完成，不代表 Gate 0 PASS |
| `2` | 已生成审计证据，但存在阻塞 |
| `64` | CLI 或输入合同非法 |
| `66` | snapshot、schema、hash 或只读来源不满足合同 |

底层 `sqlite3.Error/OSError` 会归一为固定的 `SOURCE_SQLITE_ERROR/SOURCE_IO_ERROR`，stderr 不输出数据库路径、SQL 或原始异常文本。

即使未来出现 exit 0，也不能据此声明 `CONTROLLED_FEASIBLE`；Gate 0 仍需要权限、受控分流、allocation、冻结 qualification rule、PowerAssessment 和签字 Receipt。

## 6. Reason codes

```text
MISSING_META_AD_ID
MISSING_META_ID
META_ID_INVALID
AD_NOT_IN_FACTS
AD_NOT_IN_EXPERIMENT
META_ID_CHAIN_MISMATCH
AMBIGUOUS_FACT_LINEAGE
AMBIGUOUS_EXPERIMENT_AD_ID
MISSING_STUDY_ID
STUDY_ID_INVALID
MISSING_STUDY_CELL_ID
STUDY_CELL_ID_INVALID
STUDY_CELL_NOT_READBACK_VERIFIED
STUDY_ID_NOT_SHARED
DUPLICATE_STUDY_CELL_ID
EVENT_TIME_PRECISION_INSUFFICIENT
RAW_PAYLOAD_INVALID
RAW_PAYLOAD_HASH_MISMATCH
MISSING_CANONICAL_IDENTITY
CANONICAL_IDENTITY_INVALID
CANONICAL_IDENTITY_NOT_IN_CRM
AMBIGUOUS_CANONICAL_IDENTITY
LEAD_CUSTOMER_LINK_CONFLICT
MISSING_CRM_VERIFICATION_TIMESTAMP
EXPERIMENT_SCOPE_INCOMPLETE
SOURCE_LIMIT_EXCEEDED
SOURCE_DRIFTED
SOURCE_SIDECAR_DRIFTED
QUALIFICATION_RULE_UNFROZEN
READBACK_PROVENANCE_UNAUDITED
```

## 7. 明确排除项

- 不读取或复制生产数据库；snapshot 由授权流程另行提供。
- 不建表、迁移、建索引、回填或修改任何 DB。
- 不接 API、worker、scheduler、Meta adapter。
- 不做名称、手机号、时间或比例归因。
- 不实现 qualified-join 业务判断。
- 不计算 PowerAssessment，不生成 Gate Receipt。
- 不对账 Plan / Approval / Verification / Receipt；该职责留给 G0-04/G0-05。
- 不执行 Meta GET/POST，不创建或启用广告。
- 不宣称 `CONTROLLED_FEASIBLE`、Gate 0 PASS 或第一阶段完成。
