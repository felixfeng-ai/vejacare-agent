"""后台鉴权的接口级测试。

对应 specs/001 的 AC2–AC6。这里刻意用**匿名 client**打受保护接口，
因为要验的正是"没有令牌会怎样"——用带令牌的夹具就把被测行为短路了。

口令、密钥来自 conftest 顶部的环境变量，与生产无关。
"""

from __future__ import annotations

import time

import pytest
from fastapi import Request
from httpx import ASGITransport, AsyncClient

from app.security import (
    LOGIN_LOCK_SECONDS,
    MAX_LOGIN_FAILURES,
    ROLE_ADMIN,
    ROLE_AGENT,
    issue_token,
)

pytestmark = pytest.mark.asyncio

AGENT_PASSWORD = "test-agent-pwd"
ADMIN_PASSWORD = "test-admin-pwd"

#: 所有受保护接口，逐条验。(方法, 路径, 需要什么角色)
PROTECTED = [
    ("get", "/api/console/me", ROLE_AGENT),
    ("get", "/api/escalations", ROLE_AGENT),
    ("get", "/api/escalations/esc-whatever", ROLE_AGENT),
    ("post", "/api/escalations/esc-whatever/reply", ROLE_AGENT),
    ("post", "/api/escalations/esc-whatever/resolve", ROLE_AGENT),
    ("get", "/api/metrics/satisfaction", ROLE_ADMIN),
]


async def _call(client: AsyncClient, method: str, path: str, **kwargs):
    return await client.request(method.upper(), path, json=kwargs.get("json"))


# ---------------------------------------------------------------- AC2 无令牌


@pytest.mark.parametrize("method,path,role", PROTECTED)
async def test_protected_route_without_token_is_401(
    client: AsyncClient, method: str, path: str, role: str
):
    """不带任何凭证 → 401，且响应体仍是统一信封。

    信封这一条不能省：前端只写了一套解包逻辑，鉴权失败要是漏出别的形状，
    用户看到的就是"网络错误"而不是"请先登录"。
    """
    resp = await _call(client, method, path, json={"reply": "x"})
    assert resp.status_code == 401, resp.text

    body = resp.json()
    assert body["success"] is False
    assert body["data"] is None
    assert body["error"]["code"] == "CONSOLE_UNAUTHORIZED"
    assert body["error"]["message"]


async def test_garbage_token_is_401(client: AsyncClient):
    """乱填的令牌 → 401。"""
    resp = await client.get(
        "/api/escalations", headers={"Authorization": "Bearer not-a-real-token"}
    )
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "CONSOLE_TOKEN_INVALID"


async def test_malformed_authorization_header_is_401(client: AsyncClient):
    """缺 Bearer 前缀、缺值、空值——都当没带令牌处理，不是 500。"""
    for header in ("", "abc", "Bearer", "Bearer   ", "Basic dXNlcjpwYXNz"):
        resp = await client.get("/api/escalations", headers={"Authorization": header})
        assert resp.status_code == 401, f"header={header!r} 应当 401"


# ---------------------------------------------------------------- AC3 登录


async def test_login_with_agent_password_returns_token(client: AsyncClient):
    """口令正确 → 返回令牌，令牌内含角色与过期时间。"""
    resp = await client.post("/api/console/login", json={"password": AGENT_PASSWORD})
    body = resp.json()
    assert resp.status_code == 200, resp.text
    assert body["success"] is True

    data = body["data"]
    assert data["token"]
    assert data["role"] == ROLE_AGENT
    assert data["expires_at"] > time.time(), "过期时间必须在未来"


async def test_login_with_admin_password_returns_admin_role(client: AsyncClient):
    resp = await client.post("/api/console/login", json={"password": ADMIN_PASSWORD})
    assert resp.json()["data"]["role"] == ROLE_ADMIN


async def test_login_response_never_echoes_the_password(client: AsyncClient):
    """响应里不能回显口令——它会进浏览器历史、进日志、进快照。"""
    resp = await client.post("/api/console/login", json={"password": AGENT_PASSWORD})
    assert AGENT_PASSWORD not in resp.text


async def test_login_with_wrong_password_is_401(client: AsyncClient):
    resp = await client.post("/api/console/login", json={"password": "猜的"})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "CONSOLE_BAD_PASSWORD"


async def test_login_error_is_identical_for_both_passwords(client: AsyncClient):
    """口令错与角色不存在给出同一句话。

    区分开就等于告诉试探者"这个前缀是对的"，把穷举空间砍掉一大截。
    这里顺带覆盖"客服口令错"与"管理口令错"两条路径给出同样响应。
    """
    wrong_agent = await client.post("/api/console/login", json={"password": "test-agent-pw"})
    wrong_admin = await client.post("/api/console/login", json={"password": "test-admin-pw"})
    assert wrong_agent.json()["error"] == wrong_admin.json()["error"]


async def test_login_with_agent_token_grants_escalation_access(agent_client: AsyncClient):
    """口令换来的令牌真的能用——否则登录只是个摆设。"""
    resp = await agent_client.get("/api/escalations")
    assert resp.status_code == 200, resp.text
    assert resp.json()["success"] is True


async def test_me_returns_identity(agent_client: AsyncClient):
    """/me 让前端刷新后恢复登录态，不必再问一次口令。"""
    resp = await agent_client.get("/api/console/me")
    body = resp.json()
    assert resp.status_code == 200, resp.text
    assert body["data"]["role"] == ROLE_AGENT
    assert body["data"]["name"]
    assert "token" not in body["data"], "身份接口不该把令牌再吐回来"


# ---------------------------------------------------------------- AC4 跨角色


async def test_agent_token_is_403_on_metrics(agent_client: AsyncClient):
    """客服能办工单，但看不到经营看板。"""
    resp = await agent_client.get("/api/metrics/satisfaction")
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "CONSOLE_FORBIDDEN"


async def test_admin_token_can_reach_agent_routes(admin_client: AsyncClient):
    """管理口令覆盖客服的全部能力——权限矩阵里"管理者"那一行要立得住。"""
    resp = await admin_client.get("/api/escalations")
    assert resp.status_code == 200, resp.text


async def test_admin_token_can_read_metrics(admin_client: AsyncClient):
    resp = await admin_client.get("/api/metrics/satisfaction")
    assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------- AC5 篡改与过期


async def test_tampered_payload_is_rejected(client: AsyncClient):
    """把 payload 改成 admin 但不动签名 → 401。签名防的就是这个。"""
    import base64
    import json

    token, _ = issue_token(ROLE_AGENT, "客服")
    payload_b64, _, signature = token.partition(".")

    forged = base64.urlsafe_b64encode(
        json.dumps({"role": ROLE_ADMIN, "name": "黑客", "exp": int(time.time()) + 9999}).encode()
    ).rstrip(b"=").decode()

    resp = await client.get(
        f"/api/metrics/satisfaction",
        headers={"Authorization": f"Bearer {forged}.{signature}"},
    )
    assert resp.status_code == 401, "改了 payload 却没改签名，必须被拒"


async def test_token_signed_with_other_secret_is_rejected(client: AsyncClient):
    """用别的密钥签出来的令牌，签名对不上 → 401。

    这是"猜到格式就能伪造"这条路的正面堵口：格式是公开的，密钥不是。
    """
    import base64
    import hashlib
    import hmac
    import json

    payload = {"role": ROLE_ADMIN, "name": "冒充", "exp": int(time.time()) + 9999}
    payload_b64 = (
        base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    )
    forged_sig = hmac.new(b"wrong-secret", payload_b64.encode(), hashlib.sha256).hexdigest()

    resp = await client.get(
        "/api/escalations",
        headers={"Authorization": f"Bearer {payload_b64}.{forged_sig}"},
    )
    assert resp.status_code == 401


async def test_expired_token_is_401(client: AsyncClient):
    """过期令牌 → 401。用负数 TTL 直接签一个已经过期的，不靠 sleep。"""
    token, expires_at = issue_token(ROLE_AGENT, "客服", ttl_minutes=-1)
    assert expires_at < time.time()

    resp = await client.get("/api/escalations", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "CONSOLE_TOKEN_INVALID"


# ---------------------------------------------------------------- AC6 未配置


async def test_console_disabled_returns_503_not_200(client: AsyncClient, monkeypatch):
    """没配口令 → 503，绝不因为"没配"就放行。

    这是整个方案里最要紧的一条：漏配导致后台进不去，是运维当场能发现并修的问题；
    漏配导致工单队列敞开——里面是用户的对话原文——是没人会发现的泄露。
    """
    from app.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "console_agent_password", "")
    monkeypatch.setattr(settings, "console_admin_password", "")
    monkeypatch.setattr(settings, "console_token_secret", "")

    # 连"带一个格式完全正确的令牌"也不放行
    token, _ = issue_token(ROLE_AGENT, "客服")
    resp = await client.get("/api/escalations", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 503, "后台没配口令却放行了"
    assert resp.json()["error"]["code"] == "CONSOLE_DISABLED"

    resp = await client.get("/api/metrics/satisfaction", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 503


async def test_login_is_503_when_console_disabled(client: AsyncClient, monkeypatch):
    """后台没启用时，登录接口本身也该说清楚，而不是"口令不对"。"""
    from app.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "console_agent_password", "")
    monkeypatch.setattr(settings, "console_admin_password", "")
    monkeypatch.setattr(settings, "console_token_secret", "")

    resp = await client.post("/api/console/login", json={"password": "随便"})
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "CONSOLE_DISABLED"


async def test_only_secret_without_password_is_still_disabled(client: AsyncClient, monkeypatch):
    """只有密钥没有口令 → 仍然关闭。签得出令牌不等于该放人进来。"""
    from app.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "console_agent_password", "")
    monkeypatch.setattr(settings, "console_admin_password", "")

    resp = await client.get("/api/escalations")
    assert resp.status_code == 503


# ---------------------------------------------------------------- AC7 用户端不受影响


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/api/health"),
        ("get", "/api/sessions"),
        ("get", "/api/sessions/sess-does-not-exist/messages"),
    ],
)
async def test_user_side_routes_stay_public(client: AsyncClient, method: str, path: str):
    """用户端接口不能被误伤。

    `/api/sessions/{id}/messages` 对不存在的会话回 SESSION_NOT_FOUND（200 + 信封），
    但**绝不能**是 401——这里是匿名 client，回了 401 就说明依赖加错了地方。
    """
    resp = await _call(client, method, path)
    assert resp.status_code != 401, f"{path} 被鉴权误伤了：{resp.text}"
    assert resp.status_code != 503, f"{path} 被后台开关误伤了：{resp.text}"


async def test_feedback_submission_stays_public(client: AsyncClient):
    """提交评价是用户端行为，必须保持公开——受保护的只有看板。"""
    resp = await client.post(
        "/api/feedback", json={"session_id": "sess-nonexistent", "rating": 5}
    )
    assert resp.status_code != 401, resp.text
    # 会话不存在会被业务拒绝，但那是业务错误，不是鉴权错误
    assert resp.json()["error"]["code"] == "SESSION_NOT_FOUND"


# ---------------------------------------------------------------- 限流


async def test_repeated_failures_lock_the_source(client: AsyncClient):
    """连续撞口令 → 锁定，避免在线穷举。

    这里正好要连打 MAX_LOGIN_FAILURES 次，所以每条断言都不能提前 return。
    """
    for i in range(MAX_LOGIN_FAILURES):
        resp = await client.post("/api/console/login", json={"password": f"猜{i}"})
        assert resp.status_code == 401, f"第 {i + 1} 次失败应当是 401，实际 {resp.status_code}"

    # 第 MAX+1 次：即使这次口令是对的，也应该被锁在外面
    resp = await client.post("/api/console/login", json={"password": AGENT_PASSWORD})
    assert resp.status_code == 429, "连续失败后应当锁定，而不是继续比对口令"
    assert resp.json()["error"]["code"] == "CONSOLE_LOGIN_LOCKED"
    assert str(LOGIN_LOCK_SECONDS) in resp.json()["error"]["message"] or "秒" in resp.json()["error"]["message"]


async def test_successful_login_clears_failure_count(client: AsyncClient):
    """成功登录要把失败计数清零，否则正常用户手滑几次就被锁。"""
    for _ in range(MAX_LOGIN_FAILURES - 1):
        await client.post("/api/console/login", json={"password": "错的"})

    ok_resp = await client.post("/api/console/login", json={"password": AGENT_PASSWORD})
    assert ok_resp.status_code == 200, ok_resp.text

    # 计数已清零，再来一次错口令不该立刻触发锁定
    for _ in range(MAX_LOGIN_FAILURES - 1):
        resp = await client.post("/api/console/login", json={"password": "还是错的"})
        assert resp.status_code == 401, "计数没有被清零"


# ---------------------------------------------------------------- 令牌本身


async def test_token_roundtrip_carries_role_and_name():
    """签发 → 校验的回路，以及署名能带过去。"""
    from app.security import verify_token

    token, expires_at = issue_token(ROLE_ADMIN, "客服小美")
    identity = verify_token(token)
    assert identity is not None
    assert identity.role == ROLE_ADMIN
    assert identity.name == "客服小美"
    assert identity.expires_at == expires_at


@pytest.mark.parametrize(
    "bad",
    [
        "",
        ".",
        "abc.",
        ".abc",
        "a.b.c",
        "!!!.???",
        "no-dot-at-all",
        # 非 ASCII 必须在这里。上一版用例全是 ASCII，于是漏掉了一条真实存在的
        # 500：compare_digest 收 str 时只接受纯 ASCII，而签名直接来自请求头
        # （Starlette 按 latin-1 解码），随手塞个高位字节就抛 TypeError。
        "abc.üü",
        "中文.签名",
        "abc.ÿ",
    ],
)
async def test_malformed_tokens_are_rejected(bad: str):
    """畸形令牌返回 None 而不是抛异常——这是任何人都能打到的入口。"""
    from app.security import verify_token

    assert verify_token(bad) is None


async def test_non_ascii_token_does_not_500(client: AsyncClient):
    """非 ASCII 的令牌走完整条 HTTP 链路也不能变成 500。

    verify_token 单测过了还不够：真正要防的是「任何人拿 curl 就能打出一个稳定的
    500」，那是一个可用的探测口。所以这里从接口进。
    """
    # 必须按**原始字节**发。httpx 对 str 头值按 ASCII 编码，中文会直接抛
    # UnicodeEncodeError 打不出去；而真实世界里 Starlette 是按 latin-1 解码头部的，
    # 客户端塞一个高位字节就能造出非 ASCII 的 str —— 这才是要复现的那条路径
    for raw in (b"abc.\xfc\xfc", b"\xd6\xd0\xce\xc4.\xc7\xa9\xc3\xfb"):
        resp = await client.get(
            "/api/escalations", headers={"Authorization": b"Bearer " + raw}
        )
        assert resp.status_code == 401, f"{raw!r} 打出了 HTTP {resp.status_code}"


async def test_token_with_bad_expiry_type_is_rejected():
    """exp 不是整数就不能放行。

    否则 `{"exp": "never"}` 之类会让比较抛 TypeError，变成 500——
    一个能让任何人触发的 500 就是一个可用的探测口。
    """
    import base64
    import hashlib
    import hmac
    import json

    from app.config import get_settings
    from app.security import verify_token

    secret = get_settings().console_token_secret
    payload = {"role": ROLE_ADMIN, "name": "x", "exp": "never"}
    payload_b64 = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    sig = hmac.new(secret.encode(), payload_b64.encode(), hashlib.sha256).hexdigest()

    assert verify_token(f"{payload_b64}.{sig}") is None
