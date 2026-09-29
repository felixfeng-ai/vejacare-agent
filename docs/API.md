# VeyaCare 跨境 AI 客服 Agent — 前后端接口契约

> 本文件是前后端唯一契约。前端只依赖本文件，不依赖后端实现细节。
> 后端基址：开发 `http://127.0.0.1:8000`，生产由 `VITE_API_BASE` 注入。

---

## 0. 通用约定

- 所有请求/响应体均为 `application/json; charset=utf-8`
- 时间统一 ISO 8601 UTC 字符串，例：`2026-09-28T07:31:22.114Z`
- 错误统一信封（Repository 模式统一出口）：

```json
{ "success": false, "data": null, "error": { "code": "SESSION_NOT_FOUND", "message": "会话不存在" } }
```

- 成功统一信封：

```json
{ "success": true, "data": { }, "error": null }
```

- **例外**：`/api/chat/stream` 返回 SSE 流，不使用信封。

---

## 1. 会话与聊天

### 1.1 `POST /api/chat/stream` — 流式对话（核心）

请求体：

```json
{
  "session_id": "9f1c...-uuid",   // 可选，不传则后端新建并在 session 事件里返回
  "message": "我的包裹到哪了？订单号 SO20260928001",
  "locale": "zh-CN"               // 可选，默认 zh-CN；预留多语言
}
```

响应：`text/event-stream`。每个事件形如 `data: {json}\n\n`，**事件类型由 `type` 字段区分**，前端按 `type` 分发。

| `type` | 载荷字段 | 说明 |
|--------|----------|------|
| `session` | `session_id` | 流的第一帧，前端据此落库 session_id |
| `node` | `node`, `label` | 图节点开始执行，用于展示"思考中"进度条。`node` ∈ `intent_classifier` / `kb_retriever` / `tool_executor` / `responder` / `turn_evaluator` / `escalation`；`label` 是中文可读文案 |
| `intent` | `intent`, `intent_label`, `slots` | 意图识别结果。`intent` 见下表枚举；`slots` 形如 `{"order_no":"SO...","tracking_no":null,"sku":null}` |
| `kb` | `docs[]` | RAG 命中片段。每项 `{ "title": str, "source": str, "score": float, "snippet": str }`，仅用于前端"参考资料"折叠面板 |
| `tool` | `name`, `status`, `args`, `result`, `summary` | 工具调用。`status` ∈ `running` / `done` / `error`。`running` 时只有 `args`；`done` 时带 `result`(对象) 与 `summary`(中文一句话) |
| `token` | `text` | 回复正文的增量片段，前端追加渲染（打字机效果） |
| `escalated` | `reason`, `reason_label`, `ticket_id` | 已转人工，本轮不再有 `token`；前端展示转人工横幅 |
| `feedback_request` | `message_id` | 后端判定会话可结束，前端弹出满意度评分卡 |
| `done` | `message_id`, `intent`, `escalated` | 本轮结束哨兵，前端关闭 loading |
| `error` | `code`, `message` | 出错，前端用 `message` 提示用户 |

**槽位（`slots`）的抽取口径**（2026-09-28 修正，此前线上踩过）：

- 只从**用户最新一句话**里抽取。上文出现过的订单号不会自动变成本轮的槽位。
- 唯一例外：用户本轮明确指代上一轮那个订单/包裹（"那它到哪了"），此时沿用上文实体。
- 用户问平台通用规则（"退换货政策是什么"）时，**即使上文有订单号也必须是 `null`**。

为什么强调这条：一旦继承，工具会拿上一单的订单号去查真实数据，回复里就出现
「从退款中扣除 USD 4.99 作为退回运费」——数字全是真的，但回答的不是用户问的问题，
而用户无从分辨那串数字来自另一单。宁可让他重申订单号，也不能拿真数据答错题。

同一标识符只归属一个槽位：订单号 `SO20260928001` 形如「两个字母 + 一串数字」，
与运单号模式相撞时只填 `order_no`，`tracking_no` 保持 `null`（见上方 SSE 示例）。

意图枚举（`intent` → `intent_label`）：

| intent | 中文标签 |
|--------|----------|
| `logistics` | 物流跟踪 |
| `return_refund` | 退换货 |
| `customs_duty` | 关税政策 |
| `size_fit` | 尺码选择 |
| `payment` | 支付失败 |
| `coupon` | 优惠券 |
| `order_change` | 订单修改 |
| `human_agent` | 转人工 |
| `chitchat` | 闲聊 |

SSE 示例（节选）：

```
data: {"type":"session","session_id":"9f1c-...","created_at":"2026-09-28T07:31:22Z"}

data: {"type":"node","node":"intent_classifier","label":"正在理解您的问题"}

data: {"type":"intent","intent":"logistics","intent_label":"物流跟踪","slots":{"order_no":"SO20260928001","tracking_no":null,"sku":null}}

data: {"type":"node","node":"kb_retriever","label":"正在检索知识库"}

data: {"type":"kb","docs":[{"title":"物流时效与轨迹查询","source":"logistics.md","score":0.83,"snippet":"……"}]}

data: {"type":"tool","name":"query_logistics","status":"running","args":{"order_no":"SO20260928001"}}

data: {"type":"tool","name":"query_logistics","status":"done","result":{"tracking_no":"LP00123456789","carrier":"4PX","status":"in_transit","latest":"已到达美国洛杉矶分拨中心","eta":"2026-10-02"},"summary":"包裹已到达洛杉矶分拨中心，预计 10-02 送达"}

data: {"type":"token","text":"您的包裹"}

data: {"type":"token","text":"目前在美国洛杉矶分拨中心"}

data: {"type":"done","message_id":"m_01H...","intent":"logistics","escalated":false}
```

### 1.2 `GET /api/sessions/{session_id}/messages` — 拉取历史

```json
{ "success": true, "data": { "session_id": "…", "status": "active",
  "awaiting_human": false, "escalation_id": null,
  "messages": [ { "id":"m_1","role":"user","content":"…","intent":null,"created_at":"…","meta":{} } ] },
  "error": null }
```

`role` ∈ `user` / `assistant` / `system` / `human_agent`（人工客服回复，前端用不同样式）。
`status` ∈ `active` / `escalated` / `closed`。

`awaiting_human`：这个会话**还有未结单的工单**，前端据此继续轮询并锁住输入。
判据是「有没有未结单的工单」，不是「人工说过话没有」——人工回了一句仍算在等人工，
要等他点「结束会话」（见 2.4）。早期版本按后者判断，人工一发话轮询就停了，
他后面再说什么用户都看不到。`escalation_id` 是那张未结单工单的 id，没有则为 `null`。

### 1.3 `GET /api/sessions` — 会话列表（左侧历史栏）

`data.sessions[]`：`{ "session_id", "title", "status", "updated_at", "message_count" }`，按 `updated_at` 倒序。

### 1.4 `DELETE /api/sessions/{session_id}` — 删除会话

---

## 2. 转人工（Human-in-the-loop）

> **回复与结单是两件事。** 人工说一句话（`/reply`）不等于办完了；工单要显式结单
> （`/close`）才会关掉、用户端才会解锁。这两件事曾经被绑成一个动作，结果是人工
> 回一句「稍等，我查询下」就把工单关掉了，用户再也接不上话。

### 2.1 `GET /api/escalations?status=pending` — 人工工单队列

`data.escalations[]`：
```json
{ "id":"esc_1","session_id":"…","reason":"user_requested","reason_label":"用户主动要求人工",
  "status":"pending","created_at":"…","unread_count":2,
  "summary":"用户询问订单 SO2026… 的退款进度，AI 连续两轮未解决" }
```

`unread_count`：工单建好**之后**用户又说了几句。等待人工期间 AI 不答话，但用户补的
订单号、地址都照常落库并算进这个数——没有它，客服不主动刷新就以为对方在干等。
已结单的工单恒为 `0`。

### 2.2 `GET /api/escalations/{escalation_id}` — 工单详情（含完整上下文）

`data` 额外带 `context`：`{ "messages":[...], "intent":"…", "slots":{...}, "tool_results":[...], "kb_docs":[...] }`

### 2.3 `POST /api/escalations/{escalation_id}/reply` — 人工回复

请求体：`{ "reply": "稍等，我查询下", "agent": "客服小美" }`

后端把回复写入会话（`role = "human_agent"`）。用户可以继续补充，客服也可以连说几句。

**不结单**：响应里 `status` 仍是 `pending`，会话仍是 `escalated`，AI 仍不插话。
`closing_message` 不返回。

响应：`{ "success": true, "data": { "escalation_id":"…","status":"pending","session_id":"…" }, "error": null }`

### 2.4 `POST /api/escalations/{escalation_id}/close` — 结束会话（结单）

请求体：`{}`。

结单 → 会话状态回 `active`（前端据此停止轮询、解锁输入）→ 唤醒挂起在
`interrupt()` 的图 → 给用户补一句收尾。

`closing_message` 由后端**按模板拼**，不走 LLM。人工说完就说完了，模型没有信息可
生成，唯一的产出是复读——实测人工说「稍等，我查询下」，AI 收尾把这句原样抄了一遍。
人工一句话都没说就结单时为空串，不产生任何消息。

响应：
```json
{ "success": true, "data": { "escalation_id":"…","status":"resolved","session_id":"…",
  "agent":"客服小美","closing_message":"人工客服（客服小美）已处理完毕。还有其他问题随时找我。",
  "request_feedback": true }, "error": null }
```

### 2.5 `POST /api/escalations/{escalation_id}/resolve` — 同上，保留兼容

与 `/close` 同一段实现，仅为兼容保留。前端已统一走 `/close`。

---

## 3. 满意度闭环

### 3.1 `POST /api/feedback` — 提交评分

请求体：
```json
{ "session_id":"…", "message_id":"m_01H…", "rating":5, "comment":"回复很快" }
```
`rating` 为 1–5 整数，`comment` 可空。响应：`{ "success": true, "data": { "feedback_id":"fb_1" }, "error": null }`

### 3.2 `GET /api/metrics/satisfaction` — 满意度看板数据（演示用）

```json
{ "success": true, "data": {
  "total_sessions": 42, "rated_sessions": 18, "avg_rating": 4.33,
  "rating_distribution": { "1":1,"2":0,"3":2,"4":5,"5":10 },
  "escalation_rate": 0.14, "auto_resolved_rate": 0.86,
  "top_intents": [ { "intent":"logistics","count":12 } ]
}, "error": null }
```

---

## 4. 健康检查

### 4.1 `GET /api/health`

```json
{ "success": true, "data": {
  "status":"ok",
  "llm": { "provider":"deepseek", "model":"deepseek-chat", "ok": true },
  "embedding": { "provider":"dashscope", "model":"text-embedding-v4", "dim":1024, "ok": true },
  "vector_store": { "backend":"qdrant", "collection":"vejacare_kb", "points": 68, "ok": true },
  "reranker": { "provider":"none", "ok": true },
  "database": { "backend":"sqlite", "ok": true }
}, "error": null }
```

---

## 5. 前端必须实现的交互

1. **消息气泡**：用户右侧、AI 左侧、`human_agent` 左侧但带「人工客服」徽标与不同底色。
2. **打字机**：`token` 事件逐字追加；追加期间显示光标。
3. **思考过程**：`node` 事件渲染为一条可折叠的进度链（如「正在理解您的问题 → 正在检索知识库 → 正在查询物流」），`done` 后自动折叠。
4. **工具调用卡片**：`tool` 事件在思考链里渲染成一张小卡片，`running` 转圈、`done` 展示 `summary`。
5. **参考资料**：`kb` 事件渲染为回复下方可展开的「参考资料（N）」。
6. **转人工**：`escalated` 事件后，输入框上方出现横幅「已为您转接人工客服」并禁用发送，直到收到人工回复。
7. **满意度**：`feedback_request` 事件弹出 1–5 星 + 备注输入，提交后收起并显示「感谢您的评价」。
8. **历史会话**：左侧栏列出 `/api/sessions`，可切换、可删除；本地缓存当前 `session_id`。
9. **移动端**：≤768px 时左侧栏收起为抽屉，气泡宽度自适应。
10. **错误态**：收到 `error` 事件时气泡内展示错误文案 + 重试按钮。
11. **后台入口分离**：用户端页面上**不得出现**工单台与看板的任何入口；两者各自在 `/desk`、`/admin` 路由下，由守卫按角色放行。

---

## 6. 后台鉴权（客服工作台 / 管理看板）

后台用**共享口令**换**签名令牌**，没有账号体系：口令配在环境变量（`CONSOLE_*`，见 `.env.example`），
客服一套、管理一套。令牌是 HMAC-SHA256 签名的 `payload.signature`，前端原样存起来、原样带回来。

### 6.1 `POST /api/console/login` — 口令换令牌

请求体：`{ "password": "…", "name": "客服小美" }`（`name` 可选，留空则用角色名）

```json
{ "success": true, "data": {
  "token": "eyJyb2xlIjoiYWdlbnQifQ.…", "role": "agent", "name": "客服小美", "expires_at": 1790656000
}, "error": null }
```

`role` 只有 `agent`（客服，能办工单）与 `admin`（管理员，另可看经营看板）两种。
`expires_at` 是 unix 秒，前端据此提前判过期，省掉一次注定 401 的请求。

失败（**响应体仍是统一信封，但 HTTP 状态码是真的**，见 6.4）：
`401 CONSOLE_BAD_PASSWORD`「访问口令不正确」——**不区分是哪个口令错**；
`429 CONSOLE_LOGIN_LOCKED`——同一来源连续失败 5 次锁 60 秒；
`503 CONSOLE_DISABLED`——服务端没配后台口令。

### 6.2 `GET /api/console/me` — 令牌换身份

`data`：`{ "role":"agent", "name":"客服小美", "expires_at":1790656000 }`（不含 `token`，也不需要）。
前端刷新页面后用它确认本地令牌还有效——「本地没过期」不等于「后端认」。

### 6.3 受保护接口

| 接口 | 最低角色 |
|------|----------|
| `GET /api/escalations`（2.1） | 客服 |
| `GET /api/escalations/{id}`（2.2） | 客服 |
| `POST /api/escalations/{id}/reply`（2.3） | 客服 |
| `POST /api/escalations/{id}/close`（2.4） | 客服 |
| `POST /api/escalations/{id}/resolve`（2.5） | 客服 |
| `GET /api/metrics/satisfaction`（3.2） | **管理员** |
| `GET /api/console/me`（6.2） | 客服 |

令牌通过请求头传递：`Authorization: Bearer <token>`。

**不在表里的一律公开**——尤其是 `POST /api/feedback`（3.1）：用户提交评价不需要登录，
别因为「它和看板在同一个 router 下」就顺手加依赖。

### 6.4 状态码语义

| 状态码 | `error.code` | 含义 | 前端应做 |
|--------|--------------|------|----------|
| 401 | `CONSOLE_UNAUTHORIZED` | 没带令牌 | 跳登录页 |
| 401 | `CONSOLE_TOKEN_INVALID` | 令牌签名不对/已过期/格式坏 | 清掉本地令牌，跳登录页 |
| 403 | `CONSOLE_FORBIDDEN` | 令牌有效但角色不够 | 就地提示「无权访问」，**不跳登录页** |
| 503 | `CONSOLE_DISABLED` | 服务端未配置后台口令 | 提示联系运维配置 `CONSOLE_*`，跳登录页也没用 |

这是**全项目唯一的例外**：其余接口一律 HTTP 200 + 信封表达业务错误（见 0 节）。
鉴权必须让 401/403 真实可见——否则浏览器、代理、监控都看不出这是一次未授权访问。
