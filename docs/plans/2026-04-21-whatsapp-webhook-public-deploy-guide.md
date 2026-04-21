# WhatsApp Webhook 公网部署指南

更新时间：2026-04-21
目标：尽快把 `app/official_webhook_bridge_app.py` 部署成一个真实可用的公网 webhook URL，供 Meta/WhatsApp Developer 后台填写。

---

## 1. 你最终要拿到什么

你最终要拿到的不是“WhatsApp 发给你的 URL”，而是你自己部署出来的这个地址：

- `https://your-domain.com/webhooks/whatsapp`

其中：
- `https://your-domain.com` = 你部署后得到的公网 HTTPS 域名
- `/webhooks/whatsapp` = 我已经在代码里固定好的路径

这个地址就是你要填到 Meta Developer 后台里的 Callback URL。

---

## 2. 代码入口

最小 webhook 服务入口已经做好：
- `app/official_webhook_bridge_app.py`

支持：
1. `GET /healthz`
2. `GET /webhooks/whatsapp`
   - 用于 Meta 验证 webhook
3. `POST /webhooks/whatsapp`
   - 接收事件回调
4. `GET /ops/whatsapp-webhook/latest`
   - 运营/调试查看最近一条 webhook

环境变量：
- `WHATSAPP_WEBHOOK_VERIFY_TOKEN`

---

## 3. 本地先跑通

先在本地验证服务能启动：

```bash
cd /Users/chauncey/work/mcn-ai-automation
. .venv/bin/activate
export WHATSAPP_WEBHOOK_VERIFY_TOKEN=replace-with-your-token
uvicorn app.official_webhook_bridge_app:create_app --factory --host 0.0.0.0 --port 8091
```

本地检查：

```bash
curl -s http://127.0.0.1:8091/healthz
```

预期：
```json
{"ok":true,"verify_token_configured":true,"has_latest_event":false}
```

模拟 Meta 验证请求：

```bash
curl -i "http://127.0.0.1:8091/webhooks/whatsapp?hub.mode=subscribe&hub.verify_token=replace-with-your-token&hub.challenge=123456"
```

预期：
- HTTP 200
- body 为 `123456`

---

## 4. 最快部署方案：Render

如果你只是想尽快得到一个公网 HTTPS URL，优先用 Render。

### 4.1 在 Render 新建 Web Service

仓库：
- 直接连接当前仓库，或把当前项目 push 到你可用的 Git 仓库

关键配置：
- Runtime: Python
- Build Command:
  ```bash
  pip install -r requirements.txt
  ```
- Start Command:
  ```bash
  uvicorn app.official_webhook_bridge_app:create_app --factory --host 0.0.0.0 --port $PORT
  ```

环境变量：
- `WHATSAPP_WEBHOOK_VERIFY_TOKEN=你准备填到 Meta 后台的 token`

部署成功后会得到类似：
- `https://your-service.onrender.com`

那么你的 webhook URL 就是：
- `https://your-service.onrender.com/webhooks/whatsapp`

### 4.2 部署后验证

检查 health：
```bash
curl -s https://your-service.onrender.com/healthz
```

检查 Meta 验证接口：
```bash
curl -i "https://your-service.onrender.com/webhooks/whatsapp?hub.mode=subscribe&hub.verify_token=你的token&hub.challenge=123456"
```

预期：
- HTTP 200
- body = `123456`

---

## 5. 服务器部署方案（你有自己的云服务器时）

如果你有自己的云服务器：

### 5.1 启动服务

```bash
cd /Users/chauncey/work/mcn-ai-automation
. .venv/bin/activate
export WHATSAPP_WEBHOOK_VERIFY_TOKEN=replace-with-your-token
uvicorn app.official_webhook_bridge_app:create_app --factory --host 127.0.0.1 --port 8091
```

### 5.2 用 Nginx 反代

示例：

```nginx
server {
    listen 443 ssl;
    server_name your-domain.com;

    location / {
        proxy_pass http://127.0.0.1:8091;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

完成后：
- `https://your-domain.com/webhooks/whatsapp`
就是正式 webhook URL。

---

## 6. Meta Developer 后台怎么填

在 Meta Developer / WhatsApp Business Platform 配置 Webhooks 时：

### Callback URL
填：
- `https://your-domain.com/webhooks/whatsapp`

### Verify Token
填：
- 与 `WHATSAPP_WEBHOOK_VERIFY_TOKEN` 完全一致的值

例如你服务里设的是：
- `WHATSAPP_WEBHOOK_VERIFY_TOKEN=abc123xyz`

那后台也必须填：
- `abc123xyz`

### 验证机制
Meta 会发 GET 请求：
- `hub.mode=subscribe`
- `hub.verify_token=...`
- `hub.challenge=...`

如果 token 对上，服务会直接原样返回 challenge。
这一步我已经在代码里实现好了。

---

## 7. 部署完成后的最小联调清单

### 必过项
1. `GET /healthz` 返回 200
2. `GET /webhooks/whatsapp?...` 能回 challenge
3. Meta 后台 webhook 验证通过
4. `POST /webhooks/whatsapp` 能接收 JSON
5. `GET /ops/whatsapp-webhook/latest` 能看到最近一次事件摘要

### 推荐用 curl 手测一次 POST

```bash
curl -X POST https://your-domain.com/webhooks/whatsapp \
  -H 'Content-Type: application/json' \
  -d '{
    "object": "whatsapp_business_account",
    "entry": [
      {
        "id": "waba_1",
        "changes": [
          {
            "field": "messages",
            "value": {
              "metadata": {
                "display_phone_number": "12345",
                "phone_number_id": "pnid_1"
              },
              "messages": [
                {
                  "from": "628111111111",
                  "id": "wamid-1",
                  "type": "text"
                }
              ]
            }
          }
        ]
      }
    ]
  }'
```

然后：

```bash
curl -s https://your-domain.com/ops/whatsapp-webhook/latest
```

应该能看到摘要里有：
- `object=whatsapp_business_account`
- `entry_count=1`
- `message_count=1`

---

## 8. 这一步和官方群审批 bridge 的关系

要分清两件事：

### A. Meta 官方 webhook URL
这是为了：
- 让 Meta 把 WhatsApp 事件回调给你

当前服务：
- `app/official_webhook_bridge_app.py`
已经能解决这个问题

### B. 官方群审批 bridge URL
这是为了：
- 让我们当前的官方群自动审批后端去调用“审批执行器”

当前配置是：
- `OFFICIAL_GROUP_APPROVAL_WEBHOOK_URL`

这是另一条链路，不是 Meta 回调地址本身。

所以：
- 你现在可以先把 A 做出来，拿到真实 webhook URL
- 然后再继续打通 B，把官方群审批 bridge 接上

---

## 9. 结论

如果你现在想“立刻拥有一个 webhook URL”，最短路径就是：

1. 部署 `app.official_webhook_bridge_app.py`
2. 设置 `WHATSAPP_WEBHOOK_VERIFY_TOKEN`
3. 拿到公网 HTTPS 域名
4. 使用：
   - `https://your-domain.com/webhooks/whatsapp`

这就是你要填进 Meta Developer 后台的真实 webhook URL。
