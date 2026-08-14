# MCN 每日新增主播交接契约 v1

## 边界

MCN 是每日新增结果的唯一计算方。牛马中台只消费已完成、不可变且带版本的名单，不自行按主播档案或公会数据重算。

业务键为 `platform + businessDate + revision`。同一平台和业务日首次正式结果为 revision 1；名单内容改变时创建新 revision 并发送 `revised` 事件，旧 revision 永不覆盖。

## 日期口径

| platform | dateContract | 口径 |
| --- | --- | --- |
| `LINKY` | `linky_created_at_utc_date_v1` | Linky 主播 `createdAt` 的 UTC 自然日，使用 MCN 已冻结的日名单 |
| `TIMO` | `timo_join_time_beijing_date_v1` | Timo `getHostList.joinTime` 转北京时间后的自然日，即加入当前公会时间，不是平台注册时间 |

## 主动事件

`POST /api/internal/mcn/newcomers/events`

请求头：

- `x-mcn-event-id`: 全局幂等事件 ID
- `x-mcn-timestamp`: Unix 秒
- `x-mcn-signature`: `sha256=<hex>`，其中 hex 为 `HMAC-SHA256(secret, timestamp + "." + raw_body)`

接收方仅在 HTTP 202 且 JSON `{"ok":true}` 时确认成功；`duplicate:true` 同样视为成功。MCN 单次发送最多 3 次指数退避，持久 outbox 最多 8 次调度尝试，超过后进入 `dead`，不会无限重试。

事件类型：

- `mcn.newcomers.daily.completed`
- `mcn.newcomers.daily.revised`
- `mcn.newcomers.daily.failed`

完成/修订事件携带 `platform`、`businessDate`、`dateContract`、`revision`、`checksum`、`expectedGuildCount`、`completedGuildCount`、`summaryCount`、`rosterCount`、`uniqueIdCount`、源 publication 的 `completedAt`、`publishedAt` 和 `consumable=true`。失败事件 `consumable=false` 并携带失败公会，不允许消费半成品；从未成功发布时其 revision 为 0。

## 只读补拉

`GET /api/external/newcomers/daily?platform=LINKY|TIMO&business_date=YYYY-MM-DD&revision=N&limit=500&offset=0`

鉴权：`Authorization: Bearer <token>`。MCN 使用 `NEWCOMER_EXTERNAL_FEED_TOKEN`；未配置时为兼容现役链路回退到 `TIMO_EXTERNAL_FEED_TOKEN` 或 `TIMO_EXTERNAL_API_TOKEN`。

revision 省略或为 0 时返回最新正式版本；指定正整数时严格返回该版本。不存在正式版本返回 HTTP 503，禁止以空数组冒充零新增。响应为：

```json
{
  "ok": true,
  "data": {
    "schemaVersion": 1,
    "platform": "TIMO",
    "businessDate": "2026-08-13",
    "revision": 1,
    "dateContract": "timo_join_time_beijing_date_v1",
    "consumable": true,
    "expectedGuildCount": 3,
    "completedGuildCount": 3,
    "summaryCount": 12,
    "rosterCount": 12,
    "uniqueIdCount": 12,
    "checksum": "...",
    "completedAt": "...",
    "rows": [
      {"guildId": "BR11501", "guildName": "Royal BR", "subjectId": "123", "sourceUserUuid": "optional"}
    ]
  }
}
```

分页请求必须固定 `revision`；接收方以 `(platform,businessDate,revision,offset)` 补拉，校验总数和 checksum 后才推进各平台独立水位。

## checksum 精确定义

1. 每行只投影 `guildId`、`guildName`、`subjectId`，`sourceUserUuid` 非空时才包含该键。
2. 按 `(subjectId, guildId, guildName)` 升序排序。
3. 以 UTF-8 JSON 序列化整个 rows 数组：对象 key 按字典序，`ensure_ascii=false`，分隔符为 `(',', ':')`，不添加空白或换行。
4. 对上述字节计算 SHA-256，输出小写十六进制。

checksum 基于完整名单而非当前分页。接收方拉齐全部分页后用同一算法验收。

## 完整性门禁

正式发布前必须同时满足：该业务日已落库任务所冻结的预期公会数等于完成公会数；每个公会汇总新增数等于名单数等于公会内唯一 ID 数；平台汇总数等于完整名单数等于平台唯一 ID 数。预期公会集合来自该日任务，不随后来新增、停用公会而漂移。任一条件不满足则事务回滚，不生成可消费版本；终态任务失败只生成 `failed` 事件。

发送配置：`NEWCOMER_WEBHOOK_URL`、`NEWCOMER_WEBHOOK_SECRET_FILE`。secret 只从权限受控文件读取，不进入环境输出、日志或事件体。

日任务在同一数据库事务内生成 publication 与 outbox 事件；合并发布时由现役五分钟通知链路调用 `python scripts/notify_newcomer_publications.py` 排空 outbox。网络发送不占用统计落库事务，发送失败只推进持久重试状态，不回滚已经正式完成的不可变 publication。
