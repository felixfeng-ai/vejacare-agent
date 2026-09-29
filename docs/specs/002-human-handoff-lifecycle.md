# 002 · 转人工的生命周期：把「人工回复」与「工单结单」拆开

> 状态：**已实现**（后端 + 工单台 + 契约校验；2026-09-29）
> 前置：[001-console-auth-and-roles.md](001-console-auth-and-roles.md)（后台鉴权与入口分离已上线）
> 后续：实时人工会话（B），本轮不做，另立 SDD

实现落点：`POST /api/escalations/{id}/close`、`unread_count`（队列与详情都带）、
`escalation_wait ──► feedback`（不再回 `responder`）、等待期间用户消息照常落库。
契约校验 `scripts/verify_contract.py` 已覆盖：回复后仍 pending、结单后 resolved、
收尾文案不是复读、等待期间的发言计入未读。

## 1. 问题

工单台第一次在真实对话里跑通之后，暴露出转人工这条链路的产品形态是错的。

用户点转人工 → 客服在 `/desk` 回一句 → **系统立刻结单，会话交还 AI**。人工只有一次发言机会。
实测截图里最刺眼的一幕：

- 人工说：`稍等，我查询下`（一句占位话，不是结论）
- AI 收尾说：`收到，人工同事已经为您处理。我这边补充一下：稍等，我查询下。如还有其他问题，随时找我。`
- 工单列表标着：**已处理**，处理结论 = `稍等，我查询下`

也就是说：**人工说"等我查一下"，系统就当处理完了并结单，谁也没查。**

## 2. 根因（三条，各自独立）

### 2.1 `interrupt()` 只能被唤醒一次，而结单和唤醒是同一个动作

`POST /api/escalations/{id}/reply`（[escalation.py](../../backend/app/api/escalation.py)）一次把三件事做完：

1. 人工回复落库
2. `resolve_escalation()` —— 工单转 `resolved`
3. `Command(resume=...)` 唤醒图

第 3 步是一次性的：唤醒后图跑到 END，**没有第二次挂起的入口**。所以"人工再补一句"在当前结构里无处可去——工单已 `resolved`，第二句会被 `ESCALATION_ALREADY_RESOLVED` 挡掉。

**结单不该是回复的副作用。** 回复是"我说了一句话"，结单是"这事办完了"，两件事被绑成一个动作，才导致"说一句占位话 = 结单"。

### 2.2 人工回话之后又让 LLM 生成收尾

`escalation_wait ──(resume)──► responder`（[builder.py](../../backend/app/graph/builder.py)）。人工说完，消息又被喂给 LLM 做"收尾"。

提示词里写了「不要重复人工已经说过的话」，但**人工那句话本身不是结论时，模型除了复读无话可说**——它没有别的信息。这里 LLM 的信息增益为零，唯一的产出就是复读。

### 2.3 等待人工期间，用户说的话被静默丢弃

[chat.py](../../backend/app/api/chat.py) 的 `escalated` 分支在 `add_message` **之前**就 `return` 了：

```
ensure_session(...)
if session.status == "escalated":
    yield 请稍候           # ← 在这里返回
    return
await repository.add_message(... role="user" ...)   # ← 永远走不到
```

用户在等待期间补的关键信息（订单号、地址、具体诉求）**既不进对话记录、也不进工单**，直接消失。这是本项目的老毛病又一次发作：**接口正常返回，数据其实没落库**（参见 001 之前修过的 `get_db` 提交语义问题）。

而且客服端**没有任何推送**：工单台只有一个"刷新"按钮。用户等待期间说的话，客服不主动刷新就永远看不到。

### 2.4 顺带：同一份原因，两处显示不一致

工单详情写「转人工原因：用户主动要求人工」，摘要却写「触发原因：**未明确**」。
`_build_summary` 读的是 state 里的 `escalation_reason`，而落库用的是 `or "user_requested"` 的兜底值——建单那一刻 state 里是空的。

## 3. 决策

**把「人工回复」和「工单结单」拆成两个独立动作。**

| | 现在 | 改后 |
|---|---|---|
| `POST /reply` | 回复 + 结单 + 唤醒图 | **只落回复**。可重复调用，工单保持 `pending` |
| 结单 | `/reply` 的副作用 | **人工显式点「结束会话」**（新接口 `POST /close`） |
| AI 收尾 | resume 后由 LLM 生成 | **确定性模板**，且只在结单时产出 |
| 等待期间用户发言 | 丢弃 | 落库，且在客服端标为"有新消息" |

### 3.1 为什么收尾不再交给 LLM

因为**没有信息可生成**。人工的最后一条回复就是结论本身，LLM 的输出只可能是它的改写或复读。去掉这一步，"AI 复读人工的话"这一整类 bug 从结构上消失，而不是靠提示词祈祷。

收尾文案由 `/close` 接口确定性拼装，例如：

> 人工客服已处理完毕：{人工最后一条回复}

人工没有回复就结单（"无需回复，直接完成"）时，不产出收尾消息。

### 3.2 为什么结单时才唤醒图（而且仍然要唤醒）

图此刻仍**挂起在 `interrupt()`**。不唤醒的话，会话被标成 `active`、图却停在中断点——用户下一条消息会撞上一个未恢复的图。**这是现在 `/resolve` 就存在的隐患**：它把会话改成 `active` 却不唤醒图，走"无需回复，直接完成"这条路会留下一个坏状态。本轮一并修掉。

### 3.3 用户端在人工已回话后能继续说话（沿用现有机制）

前端是靠「历史里出现 `human_agent` 消息」解锁输入框的（[chatReducer.ts](../../frontend/src/hooks/chatReducer.ts)）。这条不用改。

但**会话状态仍是 `escalated`**，所以用户发言后 AI 依旧不插话（避免和人工抢答），只回一句更准确的提示：人工已接手、消息已转达。**真正的实时性留给 B。**

## 4. 接口契约变化

| 接口 | 变化 |
|---|---|
| `POST /api/escalations/{id}/reply` | **行为变更**：不再结单、不再唤醒图。幂等，可连发。响应里去掉 `closing_message` / `request_feedback` |
| `POST /api/escalations/{id}/close` | **新增**：结单。唤醒图 → 产出确定性收尾 → 会话回 `active`。人工未回复时也可调用 |
| `POST /api/escalations/{id}/resolve` | **保留但收紧**：等价于不带回复的 `/close`（"无需回复，直接完成"）。不再出现"改了状态却没唤醒图" |
| `POST /api/chat/stream` | `escalated` 分支**先把用户消息落库**再返回提示 |
| `GET /api/escalations` | 列表项增加 `unread_count`（工单创建后用户又发了几条） |

失败方向沿用既有约定：`/reply` 与 `/close` 对非 `pending` 工单返回 `ESCALATION_ALREADY_RESOLVED`。

## 5. 状态机

```
pending ──reply(reply 落库，状态不变)──┐
   │                                  │  （可重复）
   │◄─────────────────────────────────┘
   │
   └──close / resolve──► resolved
                          ├─ 唤醒图（解挂起，必做）
                          ├─ 有回复 → 追加一条确定性收尾消息
                          └─ 会话状态 → active
```

## 6. 明确不做（留给 B）

- 用户消息**实时推送**到客服端（本轮只用轮询/刷新 + 未读标记）
- 人工接管期间用户消息**直达人工**（本轮仍只落库 + 提示"已转达"）
- 双端 SSE、在线状态、正在输入提示

## 7. 测试计划

- `escalated` 状态下用户发言 → **消息确实落库**（另开会话去读，只认真提交过的数据）
- `/reply` 后工单仍为 `pending`；连发两条 → 两条都在对话记录里
- `/reply` 后**没有** AI 收尾消息（复读不可能再发生）
- `/close` 后：工单 `resolved`、会话 `active`、有且仅有一条收尾消息，且**不是**人工回复的复读
- `/resolve`（无回复）后：图被唤醒、会话 `active`、无收尾消息
- 对已 `resolved` 工单再 `/reply` 或 `/close` → `ESCALATION_ALREADY_RESOLVED`
- 摘要里的触发原因与工单 `reason` 一致

## 8. 影响文件

| 文件 | 改动 |
|---|---|
| `backend/app/api/chat.py` | escalated 分支先落库 |
| `backend/app/api/escalation.py` | `/reply` 去结单；新增 `/close`；`/resolve` 收紧 |
| `backend/app/graph/builder.py` | `escalation_wait` → END（不再走 responder） |
| `backend/app/graph/nodes/escalation.py` | resume 后不再注入 SystemMessage；摘要用兜底后的 reason |
| `backend/app/db/repository.py` | 未读数查询；`resolve_escalation` 支持多次回复 |
| `frontend/src/pages/DeskPage.tsx` | 回复后不结单；新增「结束会话」按钮；未读标记 |
| `frontend/src/api/client.ts` / `types.ts` | `closeEscalation`、`unread_count` |
| `docs/API.md` | 第 5 节接口表更新 |
