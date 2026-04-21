# 真实 CRM 接入规范文档

> 目标：沉淀当前已经在真实环境中验证成功的 CRM 接入方式，作为后续把 `mcn-ai-automation` 从本地 MVP 投影层升级到真实 CRM 联调层的依据。

## 1. 当前结论

已在真实环境中验证成功：
- 登录 CRM
- 读取应用列表
- 读取公会/部门列表
- 查询客户列表
- 新增客户
- 更新客户
- 上传客户凭证图片到 OSS
- 将图片 URL 回写到 CRM 客户记录
- 将凭证状态更新为“已上传”

## 2. 访问入口

### 2.1 前端页面
- `http://47.236.9.71:7819/index.html#/login`

说明：
- 当前浏览器自动化环境下前端仍可能白屏
- 但不影响后端 API 直接接入

### 2.2 后端 API 根地址
- `http://47.236.9.71:8310/enterprise-admin`

## 3. 认证规范

### 3.1 登录接口
- `POST /login`

完整 URL：
- `http://47.236.9.71:8310/enterprise-admin/login`

请求体：
```json
{
  "username": "<crm_username>",
  "password": "<crm_password>"
}
```

成功响应示例：
```json
{
  "code": 0,
  "msg": "success",
  "data": {
    "token": "bece5a17b80fd94dab16a9c694096542",
    "expire": 43200
  }
}
```

### 3.2 鉴权头
后续请求必须使用：
```http
token: <token>
```

注意：
- 不要使用 `Authorization: Bearer ...`
- 不要只放 query token（除上传图片时有额外情况）

## 4. 基础主数据接口

### 4.1 应用列表
- `GET /customer/ywapps/allList`

完整 URL：
- `http://47.236.9.71:8310/enterprise-admin/customer/ywapps/allList`

用途：
- 根据应用名称获取 `appId`

当前已确认：
- `Linky` -> `1982695849239654401`

### 4.2 公会/部门列表
- `GET /sys/dept/allList`

完整 URL：
- `http://47.236.9.71:8310/enterprise-admin/sys/dept/allList`

用途：
- 根据公会名称获取 `deptId`

当前已确认可用部门包括：
- `Sampanye`
- `Permata`
- `Piso`

## 5. 客户查询接口

### 5.1 客户分页查询
- `GET /customer/ywcustomer/page`

完整 URL：
- `http://47.236.9.71:8310/enterprise-admin/customer/ywcustomer/page`

用途：
- 按条件查询客户是否已存在
- 查询新增/更新后的真实结果

常用查询参数：
- `ywId`
- `mobile`
- `mobile + ywId`

返回结构示例字段：
- `id`
- `mobile`
- `areaCodeAndmobile`
- `ywId`
- `appId`
- `appName`
- `wa`
- `pendaftaranGroup`
- `deptName`
- `deptId`
- `fileUrl`
- `pzStatus`
- `pzStatusText`

## 6. 客户写入接口

### 6.1 新增客户
- `POST /customer/ywcustomer`

### 6.2 更新客户
- `PUT /customer/ywcustomer`

完整 URL：
- `http://47.236.9.71:8310/enterprise-admin/customer/ywcustomer`

### 6.3 关键 payload 字段
真实验证中至少使用过这些字段：
```json
{
  "id": "<更新时需要，新增时可不传>",
  "mobile": "17705921585",
  "ywId": "199491",
  "areaCode": "62",
  "appId": "1982695849239654401",
  "appName": "Linky",
  "wa": "Piso-1",
  "pendaftaranGroup": "Piso-1",
  "deptId": "1902370735507128322",
  "deptName": "Permata",
  "remark": "Hermes CRM write test",
  "joinGroup": null,
  "paymentStatus": "",
  "pzStatus": 0,
  "userQuality": "",
  "fileUrl": ""
}
```

### 6.4 当前业务字段到 CRM 字段映射

| 业务含义 | CRM 字段 | 说明 |
|---|---|---|
| 用户手机号（来自 WhatsApp） | `mobile` | 号码主体，不含 `+` |
| 区号 | `areaCode` | 例如 `62` |
| 平台账号 ID / SID / 客户ID | `ywId` | 核心唯一识别字段 |
| 所属应用 | `appName` + `appId` | `appId` 需先从应用列表查 |
| 注册群组 | `wa` | 当前样本可直接写群组名 |
| 注册群组补充字段 | `pendaftaranGroup` | 建议同步写 |
| 所属公会 | `deptName` + `deptId` | 以公会后台实际选取为准 |
| 备注 | `remark` | 可写测试说明 / 冲突说明 |
| 凭证图片 URL | `fileUrl` | 上传成功后写入 |
| 凭证状态 | `pzStatus` | 已验证可用 `1` 表示已上传 |

## 7. 图片上传接口规范

### 7.1 上传接口
- `POST /sys/oss/upload`

完整 URL：
- `http://47.236.9.71:8310/enterprise-admin/sys/oss/upload`

### 7.2 关键前提
必须带上当前客户记录的 `id`。

正确调用方式示例：
```http
POST /enterprise-admin/sys/oss/upload?id=2043950571220840450
Header: token: <token>
Form-Data:
  file = <binary image>
```

### 7.3 关键发现
如果不带 `id`，服务端会返回：
```json
{
  "code": 500,
  "msg": "服务器内部异常",
  "data": null
}
```

带上 `id` 后，上传成功响应示例：
```json
{
  "code": 0,
  "msg": "success",
  "data": {
    "src": "http://tt-img-bucket.oss-ap-southeast-1.aliyuncs.com/20260414/26716cb965e0400f8ff0a6a62f0c9ffb.png"
  }
}
```

### 7.4 上传成功后的后续动作
上传成功后，必须再调用一次客户更新接口，把：
- `fileUrl = <src>`
- `pzStatus = 1`
写回客户记录。

回查成功标志：
- `fileUrl` 有值
- `pzStatus = 1`
- `pzStatusText = 已上传`

## 8. 当前真实业务规则

### 8.1 CRM 不是主校验闸口
当前流程应为：
1. 用户提供账号 ID 或截图
2. 去公会后台绑定
3. 只有绑定成功，才进入 CRM 入库
4. 如果绑定失败，不进 CRM，而进入客服二次沟通

### 8.2 字段来源规则
- 用户手机号：来自用户 WhatsApp 账号
- 用户账户 ID：来自用户主动报给客服/AI 的数字，或截图识别结果
- 所属公会：以公会后台实际选取/绑定的公会为准
- 所属应用：由业务预设
- 上传截图凭证：正式生产场景中，必须和该用户的文本信息一一对应

## 9. 真实验证结果摘要

### 用户1
- 真实 CRM 新增成功
- 真实 CRM 回查成功

### 用户2
- 从截图识别出 `ywId = 45689309`
- CRM 命中已有记录
- 新增被系统正常拦截
- 说明查重/旧记录命中正常

### 用户3
- 从截图识别出 `ywId = 45678991`
- 真实 CRM 新增成功
- 后续根据权限更新公会为 `Piso`
- 图片上传成功
- `fileUrl` 已回写
- `pzStatusText = 已上传`

## 10. 推荐接入顺序

### 阶段 1：半自动
- 人工完成公会后台绑定
- 系统负责 CRM 新增/更新
- 图片上传和回写为可选补充能力

### 阶段 2：更自动化
- 绑定成功后由系统自动执行 CRM 写入
- 如业务需要，再补充图片凭证自动回写

### 阶段 3：全链路编排
- account submission -> bind result -> CRM write -> group flow
- voucher upload 仅在业务需要时作为可选支线

## 11. 后续代码接入建议

在 `mcn-ai-automation` 中建议新增一个真实 CRM adapter，包含：
- `crm_login()`
- `crm_get_apps()`
- `crm_get_depts()`
- `crm_find_customer(ywId/mobile)`
- `crm_create_customer(payload)`
- `crm_update_customer(payload)`
- `crm_upload_voucher(customer_id, image_path)`
- `crm_attach_voucher(customer_payload, image_url)`

## 12. 最终结论

当前已经真实验证通过：
- CRM 真实接入可行
- 不只是查询可行，真实写入也可行
- 图片上传也可行，但必须带客户 `id`

因此，后续可以把 CRM 视为“已打通的真实外部系统”，不再只是方案层。