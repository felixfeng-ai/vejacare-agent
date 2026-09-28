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
  "messages": [ { "id":"m_1","role":"user","content":"…","intent":null,"created_at":"…","meta":{} } ] },
  "error": null }
```

`role` ∈ `user` / `assistant` / `system` / `human_agent`（人工客服回复，前端用不同样式）。
`status` ∈ `active` / `escalated` / `closed`。

### 1.3 `GET /api/sessions` — 会话列表（左侧历史栏）

`data.sessions[]`：`{ "session_id", "title", "status", "updated_at", "message_count" }`，按 `updated_at` 倒序。

### 1.4 `DELETE /api/sessions/{session_id}` — 删除会话

---

## 2. 转人工（Human-in-the-loop）

### 2.1 `GET /api/escalations?status=pending` — 人工工单队列

`data.escalations[]`：
```json
{ "id":"esc_1","session_id":"…","reason":"user_requested","reason_label":"用户主动要求人工",
  "status":"pending","created_at":"…","summary":"用户询问订单 SO2026… 的退款进度，AI 连续两轮未解决" }
```

### 2.2 `GET /api/escalations/{escalation_id}` — 工单详情（含完整上下文）

`data` 额外带 `context`：`{ "messages":[...], "intent":"…", "slots":{...}, "tool_results":[...], "kb_docs":[...] }`

### 2.3 `POST /api/escalations/{escalation_id}/reply` — 人工回复并让 Agent 继续

请求体：`{ "reply": "已为您加急，预计 24 小时内更新物流", "agent": "客服小美" }`

后端把人工回复写入会话（`role = "human_agent"`），并 `resume` 挂起的 LangGraph 图，Agent 接手做收尾（例如致歉+后续承诺）。

响应：`{ "success": true, "data": { "escalation_id":"…","status":"resolved","session_id":"…" }, "error": null }`

### 2.4 `POST /api/escalations/{escalation_id}/resolve` — 仅标记完成（不回复）

请求体：`{}`。响应同上。

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
