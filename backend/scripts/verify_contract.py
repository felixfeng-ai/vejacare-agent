"""对线上部署跑一遍契约校验：把前端会读的每个字段跟真实返回逐一比对。

为什么需要它：`avg_rating` 那个崩，类型声明写的是 `number`，后端在没有评价时
返回 `null`，而 TypeScript 的类型是手写断言、不是从接口推导出来的 —— 编译期
查不出来，前端又没有跑在 CI 里的测试，于是直到有人点开看板才炸。

这个脚本用真实 HTTP 打真实接口，检查的是「前端假设的类型」与「后端实际给的
类型」是否一致。凡是前端假设非空、后端可能给 null 的字段，都要在这里暴露。

用法：
    python scripts/verify_contract.py                    # 默认打线上
    python scripts/verify_contract.py http://127.0.0.1:3002

后台接口需要口令，从环境变量读：
    CONSOLE_AGENT_PASSWORD=... CONSOLE_ADMIN_PASSWORD=... python scripts/verify_contract.py

口令**必须**提供，不提供就直接失败退出。刻意不做"没口令就跳过鉴权检查"——
那样一旦 CI 漏配，这一整组检查会安静地不执行，而输出看上去仍是"全部通过"。
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
import uuid

BASE = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "https://cs.veyawork.work"

AGENT_PASSWORD = os.environ.get("CONSOLE_AGENT_PASSWORD", "")
ADMIN_PASSWORD = os.environ.get("CONSOLE_ADMIN_PASSWORD", "")

PROBLEMS: list[str] = []
CHECKS = 0


def fail(where: str, msg: str) -> None:
    PROBLEMS.append(f"{where}: {msg}")


def check(where: str, cond: bool, msg: str) -> None:
    global CHECKS
    CHECKS += 1
    if not cond:
        fail(where, msg)


#: 允许为 null 的字段白名单。每一条都必须在前端有对应的 null 处理，否则会崩。
#: 后端把某个字段改成可空时，前端往往不会报错（TS 的类型是手写断言，不是从接口
#: 推导的），只会在用户点开某个页面时炸 —— 看板 avg_rating 就是这么漏过去的。
#: 所以：新增可空字段前，先把前端处理写好，再来这里登记。
NULLABLE_FIELDS: dict[str, str] = {
    "metrics.avg_rating": "MetricsPanel.tsx 判 === null 显示「暂无评分」（后端刻意区分「没评分」与「0 分」）",
}


def typed(where: str, value: object, expect: type | tuple, field: str,
          nullable: bool = False) -> None:
    """断言字段类型。

    nullable=False（默认）：null 视为问题 —— 前端会按非空使用。
    nullable=True：允许 null，但该字段必须已登记在 NULLABLE_FIELDS 里，
    否则同样记为问题（防止有人随手加个 nullable=True 把真问题掩盖掉）。
    """
    global CHECKS
    CHECKS += 1
    if value is None:
        if not nullable:
            fail(where, f"字段 {field!r} 是 null，但前端按非空使用（会崩）")
        elif field not in NULLABLE_FIELDS:
            fail(where, f"字段 {field!r} 被当作可空放行，但没登记在 NULLABLE_FIELDS 里")
        return
    if not isinstance(value, expect):
        fail(where, f"字段 {field!r} 类型是 {type(value).__name__}，期望 {expect}")


def _decode(raw: bytes) -> object:
    text = raw.decode("utf-8", "replace")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def request(method: str, path: str, body: dict | None = None,
            timeout: int = 30, token: str | None = None) -> tuple[int, object]:
    url = f"{BASE}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, _decode(resp.read())
    except urllib.error.HTTPError as exc:
        # 非 2xx 也要解析成 JSON：鉴权失败返回的仍然是统一信封，
        # 当纯文本丢掉就没法校验它的形状，而"401 的信封对不对"正是前端跳登录页的依据
        return exc.code, _decode(exc.read())


def stream_chat(session_id: str, message: str) -> list[dict]:
    """打一次 SSE 对话，返回解析后的事件列表。"""
    url = f"{BASE}/api/chat/stream"
    data = json.dumps({"session_id": session_id, "message": message}).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    events: list[dict] = []
    with urllib.request.urlopen(req, timeout=60) as resp:
        for line in resp.read().decode("utf-8", "replace").splitlines():
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload:
                try:
                    events.append(json.loads(payload))
                except json.JSONDecodeError:
                    pass
    return events


# ------------------------------------------------------------------ 信封

def check_envelope(where: str, body: object) -> object:
    check(where, isinstance(body, dict), f"响应不是对象：{type(body).__name__}")
    if not isinstance(body, dict):
        return None
    check(where, "success" in body, "缺少 success 字段")
    if body.get("success"):
        check(where, body.get("error") is None, "success=true 但 error 非空")
        return body.get("data")
    check(where, isinstance(body.get("error"), dict), "success=false 但 error 不是对象")
    return None


# ------------------------------------------------------------------ SSE

SSE_REQUIRED = {
    "session": [("session_id", str)],
    "node": [("node", str), ("label", str)],
    "intent": [("intent", str), ("intent_label", str), ("slots", dict)],
    "kb": [("docs", list)],
    "tool": [("name", str), ("status", str)],
    "token": [("text", str)],
    "escalated": [("reason", str), ("reason_label", str), ("ticket_id", str)],
    "feedback_request": [("message_id", str)],
    "done": [("message_id", str), ("escalated", bool)],
    "error": [("code", str), ("message", str)],
}


def check_events(where: str, events: list[dict]) -> None:
    check(where, len(events) > 0, "一个事件都没收到")
    seen: set[str] = set()
    for ev in events:
        etype = ev.get("type")
        check(where, isinstance(etype, str), f"事件缺 type：{ev}")
        seen.add(str(etype))
        if etype == "done":
            typed(where, ev.get("message_id"), str, "done.message_id")
            typed(where, ev.get("escalated"), bool, "done.escalated")
        if etype in SSE_REQUIRED:
            for field, expect in SSE_REQUIRED[str(etype)]:
                typed(where, ev.get(field), expect, f"{etype}.{field}")
    check(where, "session" in seen or "done" in seen, "没有 session/done 事件")

    # 槽位互斥：订单号与运单号不能被填成同一个值。
    # SO20260928001 满足运单号正则 [A-Z]{2}\d{9,13}，两个槽位曾经拿到同一个字符串，
    # 前端槽位标签上显示成「订单号：SO… 运单号：SO…」——看着像两个都查到了，
    # 实际运单号那格填的是用户自己的订单号。docs/API.md 的示例里写的本来就是
    # "tracking_no": null，是代码跑偏了。
    for ev in events:
        if ev.get("type") != "intent":
            continue
        slots = ev.get("slots")
        if isinstance(slots, dict) and slots.get("order_no"):
            check(where, not slots.get("tracking_no"),
                  f"只给了订单号 {slots['order_no']}，tracking_no 却被填成 "
                  f"{slots.get('tracking_no')!r}（同一个标识符只能归属一个槽位）")
    # 文档里出现过的 kb 事件，其 docs[].score 前端会直接 .toFixed(2)
    for ev in events:
        if ev.get("type") == "kb":
            for i, doc in enumerate(ev.get("docs") or []):
                if isinstance(doc, dict):
                    typed(where, doc.get("score"), (int, float), f"kb.docs[{i}].score")
                    typed(where, doc.get("title"), str, f"kb.docs[{i}].title")
                    typed(where, doc.get("source"), str, f"kb.docs[{i}].source")


# ------------------------------------------------------------------ 后台鉴权

#: 受保护接口：不带令牌必须 401。逐条列出来而不是抽查一条，
#: 因为"某个接口忘了加依赖"正是要防的事，抽查正好会漏掉它。
GUARDED = [
    ("GET", "/api/escalations"),
    ("GET", "/api/escalations/verify-does-not-exist"),
    ("POST", "/api/escalations/verify-does-not-exist/reply"),
    ("POST", "/api/escalations/verify-does-not-exist/close"),
    ("POST", "/api/escalations/verify-does-not-exist/resolve"),
    ("GET", "/api/metrics/satisfaction"),
]


def login(password: str, expect_role: str) -> str:
    """口令换令牌。返回空串表示失败（失败已记进 PROBLEMS）。"""
    where = f"POST /api/console/login（{expect_role}）"
    status, body = request("POST", "/api/console/login", {"password": password})
    check(where, status == 200, f"HTTP {status}：{body}")
    data = check_envelope(where, body)
    if not isinstance(data, dict):
        return ""

    typed(where, data.get("role"), str, "role")
    typed(where, data.get("expires_at"), int, "expires_at")
    check(where, data.get("role") == expect_role,
          f"角色是 {data.get('role')!r}，期望 {expect_role!r}")
    check(where, password not in json.dumps(body), "响应把口令回显出来了")
    return str(data.get("token") or "")


def check_console_auth() -> tuple[str, str]:
    """校验后台鉴权，返回 (客服令牌, 管理令牌)。"""
    print("【后台鉴权】")

    # 不带令牌一律 401，且响应体仍是统一信封
    for method, path in GUARDED:
        where = f"{method} {path}（无令牌）"
        status, body = request(method, path, {} if method == "POST" else None)
        check(where, status == 401, f"无令牌却是 HTTP {status}，鉴权没有生效")
        if status == 401:
            check_envelope(where, body)
            if isinstance(body, dict) and isinstance(body.get("error"), dict):
                check(where, body["error"].get("code") == "CONSOLE_UNAUTHORIZED",
                      f"错误码是 {body['error'].get('code')!r}")

    # 乱填的令牌同样 401
    status, _ = request("GET", "/api/escalations", token="not-a-real-token")
    check("GET /api/escalations（伪令牌）", status == 401, f"伪令牌却是 HTTP {status}")

    agent_token = login(AGENT_PASSWORD, "agent")
    admin_token = login(ADMIN_PASSWORD, "admin")

    # 跨角色：客服能办工单，看不到经营看板
    status, body = request("GET", "/api/escalations", token=agent_token)
    check("GET /api/escalations（客服令牌）", status == 200, f"HTTP {status}：{body}")

    status, body = request("GET", "/api/metrics/satisfaction", token=agent_token)
    check("GET /api/metrics/satisfaction（客服令牌）", status == 403,
          f"客服看经营看板应当是 403，实际 HTTP {status}")

    status, _ = request("GET", "/api/metrics/satisfaction", token=admin_token)
    check("GET /api/metrics/satisfaction（管理令牌）", status == 200, f"HTTP {status}")

    return agent_token, admin_token


# ------------------------------------------------------------------ 主流程

def main() -> int:
    print(f"目标：{BASE}\n")

    if not AGENT_PASSWORD or not ADMIN_PASSWORD:
        print("❌ 缺少后台口令环境变量，无法校验受保护接口。")
        print("   请设置 CONSOLE_AGENT_PASSWORD 与 CONSOLE_ADMIN_PASSWORD 后重试。")
        print("   生产口令在服务器 backend/.env 里（见 deploy/cicd-deploy.sh 的生成日志）。")
        return 1

    agent_token, admin_token = check_console_auth()
    print()

    # 1. 健康检查
    status, body = request("GET", "/api/health")
    check("GET /api/health", status == 200, f"HTTP {status}")
    data = check_envelope("GET /api/health", body)
    if isinstance(data, dict):
        typed("GET /api/health", data.get("status"), str, "status")
        typed("GET /api/health", data.get("offline_demo"), bool, "offline_demo")
        kb = data.get("knowledge")
        if isinstance(kb, dict):
            typed("GET /api/health", kb.get("documents"), int, "knowledge.documents")
            typed("GET /api/health", kb.get("chunks"), int, "knowledge.chunks")

    # 2. 普通对话
    sid = f"verify-{uuid.uuid4().hex[:8]}"
    try:
        events = stream_chat(sid, "我的包裹到哪了？订单号 SO20260928001")
        check_events("POST /api/chat/stream（普通轮）", events)
        print(f"  普通轮事件：{sorted({e.get('type') for e in events})}")

        # 3. 会话列表
        status, body = request("GET", "/api/sessions")
        check("GET /api/sessions", status == 200, f"HTTP {status}")
        data = check_envelope("GET /api/sessions", body)
        # 前端 client.ts 取的是 data.sessions（不是 data.items）
        sessions = data.get("sessions") if isinstance(data, dict) else None
        typed("GET /api/sessions", sessions, list, "sessions")
        for i, s in enumerate(sessions or []):
            for f, t in (("session_id", str), ("title", str), ("status", str),
                         ("updated_at", str), ("message_count", int)):
                typed("GET /api/sessions", s.get(f), t, f"sessions[{i}].{f}")

        # 4. 会话消息
        status, body = request("GET", f"/api/sessions/{sid}/messages")
        check("GET /api/sessions/{id}/messages", status == 200, f"HTTP {status}")
        data = check_envelope("GET /api/sessions/{id}/messages", body)
        if isinstance(data, dict):
            typed("GET messages", data.get("session_id"), str, "session_id")
            typed("GET messages", data.get("status"), str, "status")
            msgs = data.get("messages")
            typed("GET messages", msgs, list, "messages")
            for i, m in enumerate(msgs if isinstance(msgs, list) else []):
                for f, t, nul in (("id", str, False), ("role", str, False),
                                  ("content", str, False), ("intent", str, True),
                                  ("created_at", str, False), ("meta", dict, False)):
                    typed("GET messages", m.get(f), t, f"messages[{i}].{f}", nullable=nul)

        # 5. 看板（本次崩的地方）—— 需要管理令牌
        status, body = request("GET", "/api/metrics/satisfaction", token=admin_token)
        check("GET /api/metrics/satisfaction", status == 200, f"HTTP {status}")
        data = check_envelope("GET /api/metrics/satisfaction", body)
        if isinstance(data, dict):
            for f in ("total_sessions", "rated_sessions"):
                typed("GET metrics", data.get(f), int, f)
            # ★ 本次崩的就是这里：无人评分时后端返回 null，前端原来直接 .toFixed(2)
            avg = data.get("avg_rating")
            print(f"  avg_rating 实际值：{avg!r}（类型 {type(avg).__name__}）")
            typed("GET metrics", avg, (int, float), "metrics.avg_rating", nullable=True)
            typed("GET metrics", data.get("rating_distribution"), dict, "rating_distribution")
            typed("GET metrics", data.get("escalation_rate"), (int, float), "escalation_rate")
            typed("GET metrics", data.get("auto_resolved_rate"), (int, float), "auto_resolved_rate")
            typed("GET metrics", data.get("top_intents"), list, "top_intents")
            for i, item in enumerate(data.get("top_intents") or []):
                typed("GET metrics", item.get("intent"), str, f"top_intents[{i}].intent")
                typed("GET metrics", item.get("count"), int, f"top_intents[{i}].count")

        # 6. 转人工全流程
        esid = f"verify-{uuid.uuid4().hex[:8]}"
        ev2 = stream_chat(esid, "我要转人工，找你们真人客服")
        check_events("POST /api/chat/stream（转人工轮）", ev2)
        esc = next((e for e in ev2 if e.get("type") == "escalated"), None)
        check("转人工", esc is not None, "没有收到 escalated 事件")
        if esc:
            eid = esc.get("ticket_id")
            typed("转人工", eid, str, "ticket_id")
            status, body = request("GET", f"/api/escalations/{eid}", token=agent_token)
            check("GET /api/escalations/{id}", status == 200, f"HTTP {status}")
            data = check_envelope("GET /api/escalations/{id}", body)
            if isinstance(data, dict):
                # 契约里主键叫 id，不是 escalation_id。
                # 这正是客服工作台要读的形状，所以哪怕前端此刻还没接上也要校验——
                # 工作台接上以后再发现字段对不上，就已经是"线上点开就崩"了。
                typed("GET escalation", data.get("id"), str, "id")
                typed("GET escalation", data.get("status"), str, "status")
                typed("GET escalation", data.get("reason_label"), str, "reason_label")
                typed("GET escalation", data.get("unread_count"), int, "unread_count")

            # 等待人工期间用户补的话不能丢：AI 不答话，但这句话要落库，
            # 并且算进工单的未读数——客服就靠它知道用户又开口了。
            ev3 = stream_chat(esid, "订单号 SO20260928001")
            awaiting = [e for e in ev3 if e.get("type") == "error"]
            check("等待人工时用户发言被受理",
                  any(e.get("code") == "AWAITING_HUMAN" for e in awaiting),
                  f"没有收到 AWAITING_HUMAN：{awaiting}")

            status, body = request("GET", "/api/escalations?status=pending", token=agent_token)
            rows = (body.get("data") or {}).get("escalations") or []
            row = next((r for r in rows if r.get("id") == eid), None)
            check("等待期间用户的发言计入 unread_count",
                  row is not None and row.get("unread_count") == 1,
                  f"工单行={row}")

            status, body = request("POST", f"/api/escalations/{eid}/reply",
                                   {"reply": "已为您加急处理", "agent": "人工客服"},
                                   token=agent_token)
            check("POST /api/escalations/{id}/reply", status == 200, f"HTTP {status}：{body}")
            data = check_envelope("POST /api/escalations/{id}/reply", body)
            # 回复 ≠ 结单。人工可以接着说，用户端仍在等人工。
            check("回复之后工单仍是待处理",
                  isinstance(data, dict) and data.get("status") == "pending",
                  f"status={data.get('status') if isinstance(data, dict) else data}")

            status, body = request("POST", f"/api/escalations/{eid}/close", {}, token=agent_token)
            check("POST /api/escalations/{id}/close", status == 200, f"HTTP {status}")
            data = check_envelope("POST /api/escalations/{id}/close", body)
            if isinstance(data, dict):
                check("结单后工单状态为 resolved",
                      data.get("status") == "resolved", f"status={data.get('status')}")
                typed("POST close", data.get("closing_message"), str, "closing_message")
                # 收尾文案是模板拼的，不能是人工回复的复读
                check("收尾文案不是复读人工回复",
                      "已为您加急处理" not in str(data.get("closing_message")),
                      f"closing_message={data.get('closing_message')!r}")

            # 结单后接着聊：追问必须接上主题，不能继承上一轮的转人工诉求。
            #
            # 这一条是为一次线上事故加的：用户问完退换货政策，追了一句「再详细点」，
            # 因为上文里有「真人客服」，这一句被判成 human_agent，AI 当场闭嘴转人工。
            # 断言放在这里而不是只放单测，是因为它只有真模型才会错——单测跑的是桩，
            # 桩改了不代表线上那个模型也改了。这条跑在线上才算数。
            stream_chat(esid, "当前平台的退换货政策是什么，什么商品允许退换货")
            ev4 = stream_chat(esid, "再详细点")
            intents4 = [e.get("intent") for e in ev4 if e.get("type") == "intent"]
            check("追问不继承上一轮的转人工诉求",
                  bool(intents4) and intents4[0] != "human_agent",
                  f"追问意图={intents4}")
            check("追问没有被转人工",
                  not any(e.get("type") == "escalated" for e in ev4),
                  "追问又触发了转人工")
            answer4 = "".join(e.get("text", "") for e in ev4 if e.get("type") == "token")
            check("追问得到了回答（没退化成闲聊）", bool(answer4.strip()), "追问没有任何回答")

        # 7. 提交满意度（造出 avg_rating 非空的分支）
        status, body = request("POST", "/api/feedback", {
            "session_id": sid, "message_id": "verify-msg", "rating": 5,
            "comment": "契约校验",
        })
        check("POST /api/feedback", status in (200, 201), f"HTTP {status}")

        status, body = request("GET", "/api/metrics/satisfaction", token=admin_token)
        data = check_envelope("GET metrics（有评价后）", body)
        if isinstance(data, dict):
            avg = data.get("avg_rating")
            print(f"  提交评价后 avg_rating：{avg!r}（有评价时必须落到数字）")
            typed("GET metrics", avg, (int, float), "metrics.avg_rating", nullable=True)

    finally:
        request("DELETE", f"/api/sessions/{sid}")

    print()
    if PROBLEMS:
        print(f"❌ {len(PROBLEMS)} 个问题（共 {CHECKS} 项检查）：")
        for p in PROBLEMS:
            print(f"   - {p}")
        return 1
    print(f"✅ 全部通过（{CHECKS} 项检查）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
