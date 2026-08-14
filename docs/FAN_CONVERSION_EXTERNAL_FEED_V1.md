# MCN CRM 成功接粉只读增量契约 v1

## 接口与鉴权

`GET /api/external/fan-conversions/daily?updated_since=<ISO-8601>&limit=500&offset=0`

请求头：`Authorization: Bearer <token>`。复用 `NEWCOMER_EXTERNAL_FEED_TOKEN`；未配置时按现役兼容规则回退到 `TIMO_EXTERNAL_FEED_TOKEN` 或 `TIMO_EXTERNAL_API_TOKEN`。

该接口只读查询 `ops_intake_items`，不会刷新、重建或写入绑定历史投影。

## 精确成功口径

一条记录必须同时满足以下条件才可导出：

1. `system_status`（忽略大小写和首尾空格）严格为 `fully_success` 或 `success`；
2. `item_id`、`parsed_phone`、`parsed_account_id` 均非空；
3. `parsed_app` 为 `Linky` 或 `Timo`；
4. `result_code` 不含 `duplicate`，且 `result_reason` 不含 `data duplication`、`duplicate_sid` 或 `sid already exists`。

该口径与 `/api/ops/intake-workbench/binding-history-items` 的 `is_success=1` 判定一致：处理中、登记失败、CRM 失败、校验失败和重复登记都不是转化。历史页面会按 WhatsApp+账号聚合展示；本接口输出成功的源登记记录，并以 `sourceRecordKey` 幂等，不把页面展示聚合误当成源事件重算。

## 字段

```json
{
  "ok": true,
  "data": {
    "schemaVersion": 1,
    "sourceContract": "ops_intake_success_v1",
    "updatedSince": "2026-08-14T00:00:00+00:00",
    "total": 1,
    "limit": 500,
    "offset": 0,
    "hasMore": false,
    "rows": [
      {
        "sourceRecordKey": "ops_intake_item:item-123",
        "idempotencyKey": "ops_intake_item:item-123",
        "platform": "LINKY",
        "subjectId": "53322723",
        "whatsappId": "+6287722090497",
        "operatorName": "Mafubo",
        "operatorAccountKey": "cs-open-id",
        "guildName": "Carote",
        "observedAt": "2026-08-14T01:02:03+00:00",
        "sourceUpdatedAt": "2026-08-14T01:02:03+00:00"
      }
    ]
  }
}
```

- `sourceRecordKey` 同时是消费幂等键，永久取 `ops_intake_item:<item_id>`。
- `subjectId` 为登记的平台账号 ID；`whatsappId` 为规范化 WhatsApp 号码。
- `operatorName` 优先取外部客服姓名，再取本地提交用户名；`operatorAccountKey` 优先取外部客服 ID，再取本地用户 ID，可用于账号映射。
- `observedAt` 为成功处理时间，缺失时回退创建时间；`sourceUpdatedAt` 使用同一现役更新时间口径。

## 增量水位与补拉

`updated_since` 必须为 ISO-8601；为空表示全量。筛选是包含边界的 `sourceUpdatedAt >= updated_since`，接收方应在拉完所有分页后再推进水位，并用 `sourceRecordKey` 去重。因此同水位重拉只会产生幂等重复，不会漏掉相同时间戳的新记录。

排序固定为 `sourceUpdatedAt ASC, item_id ASC`，分页上限 1000。无效时间返回 HTTP 400，鉴权失败返回 HTTP 401。
