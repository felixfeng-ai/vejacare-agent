"""后台鉴权：口令换签名令牌，令牌换身份。

为什么是这个规模：后台其实只需要回答两个问题——「你是谁」和「你是客服还是管理者」。
为此引一套 JWT 库 + 用户表 + 密码哈希 + 找回密码，保护它的代码会比它保护的东西还多。
所以这里用标准库手写：签名令牌防的是**篡改**（改了角色或有效期，签名就对不上），
它**不防窥探**——payload 是明文 base64，谁拿到都能解出角色来，只是改不动。
真要上生产，下一步是把令牌换成 httpOnly cookie 并补 CSRF 防护（见 specs/001 §10）。

口令是共享口令而不是个人账号：能回答「你有没有权限」，回答不了「这件事是谁干的」。
回复工单时记客服署名是缓解手段，不是审计。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass

from fastapi import Request

from app.config import get_settings

logger = logging.getLogger(__name__)

ROLE_AGENT = "agent"
ROLE_ADMIN = "admin"
ROLE_LABELS = {ROLE_AGENT: "客服", ROLE_ADMIN: "管理员"}

#: 登录失败限流：同一来源连续失败这么多次后锁定一段时间。
#: 口令是共享的、又只有两套，不限流的话在线穷举是现实的威胁。
#: 计数放在进程内存里——本项目单进程 uvicorn，够用；多进程部署会各算各的（specs/001 §10）。
MAX_LOGIN_FAILURES = 5
LOGIN_LOCK_SECONDS = 60


class ApiHttpError(Exception):
    """带 HTTP 状态码的业务异常。

    项目里其它错误走 `fail()` 返回 HTTP 200 + success:false，那是「业务没办成」；
    鉴权失败不是业务结果，是 HTTP 语义上的未授权，必须是 401/403，
    否则前端拦截器、浏览器缓存、网关统计全都看不见它。
    响应体仍由 main.py 的处理器统一包成信封，前端照旧只认一种形状。
    """

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Identity:
    """令牌解出来的身份。"""

    role: str
    name: str
    expires_at: int


# ------------------------------------------------------------------ 令牌

def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _b64d(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def _signature(secret: str, payload_b64: str) -> str:
    digest = hmac.new(secret.encode(), payload_b64.encode(), hashlib.sha256).hexdigest()
    return digest


def issue_token(role: str, name: str, *, ttl_minutes: int | None = None) -> tuple[str, int]:
    """签发令牌，返回 (token, 过期时间戳)。"""
    settings = get_settings()
    ttl = settings.console_token_ttl_minutes if ttl_minutes is None else ttl_minutes
    expires_at = int(time.time()) + ttl * 60

    payload = {"role": role, "name": name, "exp": expires_at}
    payload_b64 = _b64e(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode())
    return f"{payload_b64}.{_signature(settings.console_token_secret, payload_b64)}", expires_at


def verify_token(token: str) -> Identity | None:
    """校验签名与有效期，通过则返回身份，否则 None。"""
    settings = get_settings()
    if not settings.console_token_secret:
        return None

    payload_b64, _, signature = token.partition(".")
    if not payload_b64 or not signature:
        return None

    # 先比签名再解内容：没验签就解析 payload，等于把攻击者控制的 JSON 喂给 json.loads。
    #
    # 两边都编码成 bytes 再比：compare_digest 收 str 时只接受纯 ASCII，而 signature
    # 直接来自请求头（Starlette 按 latin-1 解码），随手塞一个非 ASCII 字节进来就会抛
    # TypeError。那是个**任何人都能触发**的 500——一个稳定的 500 就是一个探测口。
    expected = _signature(settings.console_token_secret, payload_b64)
    if not hmac.compare_digest(expected.encode(), signature.encode("utf-8", "surrogatepass")):
        return None

    try:
        payload = json.loads(_b64d(payload_b64))
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None

    role = payload.get("role")
    expires_at = payload.get("exp")
    # role 先判是不是 str 再查表：`[] not in {}` 会抛 TypeError（不可哈希），
    # 和 exp 那边用 isinstance 挡住是同一个理由——不能让畸形输入走到抛异常那一步
    if not isinstance(role, str) or role not in ROLE_LABELS:
        return None
    if not isinstance(expires_at, int) or isinstance(expires_at, bool):
        return None
    if expires_at <= int(time.time()):
        return None

    name = payload.get("name")
    return Identity(
        role=role,
        name=name if isinstance(name, str) else ROLE_LABELS[role],
        expires_at=expires_at,
    )


def _bearer_token(request: Request) -> str | None:
    header = request.headers.get("Authorization", "")
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        return None
    return value.strip()


# ------------------------------------------------------------------ 口令

def match_password(password: str) -> str | None:
    """口令对上了就返回对应角色，否则 None。

    两套口令都要比一遍（不提前 return）：只比到一半就返回，会把「客服口令错」
    和「管理口令错」的耗时差暴露出来。用 compare_digest 是为了同样的理由——
    逐字节短路比较的耗时差异，足够把口令一个字符一个字符试出来。
    """
    settings = get_settings()
    matched: str | None = None

    supplied = password.encode()
    for role, expected in (
        (ROLE_ADMIN, settings.console_admin_password),
        (ROLE_AGENT, settings.console_agent_password),
    ):
        if not expected:
            continue
        # 必须先编码成 bytes：compare_digest 收到 str 时只接受纯 ASCII，
        # 传中文会直接抛 TypeError。而口令是任何人随手就能填的输入框，
        # 一个"输入中文就 500"的错误页就是一个现成的探测口。
        if hmac.compare_digest(supplied, expected.encode()):
            matched = role
    return matched


# ------------------------------------------------------------------ 限流

@dataclass
class _Failure:
    """某个来源的失败记录。"""

    count: int = 0
    locked_until: float = 0.0


_failures: dict[str, _Failure] = {}

#: 计数表容量上限。真实来源数量有限，正常情况下远到不了；
#: 加这个上限是因为它是一张只增不减的表——没有上限的表迟早会变成内存问题。
MAX_TRACKED_SOURCES = 4096


def _client_key(request: Request) -> str:
    """限流的计数键：请求来源。

    这里**不自己解析 `X-Forwarded-For`**。生产的 nginx 用的是
    `proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;`——追加语义，
    于是请求头会变成「客户端自带的假值, 真实 IP」，**第一段永远由请求方控制**。
    按第一段计数等于把限流的钥匙交出去：攻击者每次换一个假 IP，计数永远到不了
    阈值，共享口令就可以在线穷举（而这套方案里限流是唯一的在线穷举防线）；
    反过来，他还能挑一个受害者的 IP 去撞，把对方锁在门外。

    `request.client.host` 则来自 ASGI 服务器自己的解析：uvicorn 的
    ProxyHeadersMiddleware 会从 XFF 的**右侧**取——最右边那一段是 nginx 追加的
    真实地址，伪造不了。所以信任它，不信任请求头。
    """
    return request.client.host if request.client else "unknown"


def login_locked_for(request: Request) -> int:
    """还要锁多少秒；没锁返回 0。"""
    record = _failures.get(_client_key(request))
    if record is None:
        return 0

    remaining = record.locked_until - time.time()
    if remaining <= 0:
        return 0
    # 向上取整：还剩 0.3 秒时告诉前端"还剩 1 秒"，比"还剩 0 秒"诚实
    return int(remaining) + 1


def _prune_failures(now: float) -> None:
    """清掉已解锁且没在锁定中的记录，表满时再按解锁时间淘汰最旧的一批。"""
    for key in [k for k, r in _failures.items() if r.locked_until <= now and r.count == 0]:
        del _failures[key]

    if len(_failures) < MAX_TRACKED_SOURCES:
        return
    # 仍超容量：按「锁定最早结束」排序淘汰。正在被锁的来源会排在最后，
    # 不会因为一次突发流量就把攻击者的锁定记录挤掉
    for key, _ in sorted(_failures.items(), key=lambda item: item[1].locked_until)[
        : len(_failures) - MAX_TRACKED_SOURCES + 1
    ]:
        del _failures[key]


def record_login_failure(request: Request) -> None:
    key = _client_key(request)
    now = time.time()
    if key not in _failures:
        _prune_failures(now)

    record = _failures.setdefault(key, _Failure())
    record.count += 1
    if record.count >= MAX_LOGIN_FAILURES:
        record.locked_until = now + LOGIN_LOCK_SECONDS
        logger.warning("后台登录连续失败 %d 次，锁定 %d 秒：%s", record.count, LOGIN_LOCK_SECONDS, key)


def clear_login_failures(request: Request) -> None:
    _failures.pop(_client_key(request), None)


def reset_login_failures() -> None:
    """给测试用：清掉进程内的失败计数。"""
    _failures.clear()


# ------------------------------------------------------------------ 依赖项

def require_role(*roles: str):
    """生成一个 FastAPI 依赖项，要求令牌角色在 roles 之内。"""
    allowed = tuple(roles)

    async def dependency(request: Request) -> Identity:
        settings = get_settings()
        if not settings.console_enabled:
            raise ApiHttpError(
                503, "CONSOLE_DISABLED", "后台未配置访问口令，该功能未启用"
            )

        token = _bearer_token(request)
        if token is None:
            raise ApiHttpError(401, "CONSOLE_UNAUTHORIZED", "缺少访问令牌，请先登录")

        identity = verify_token(token)
        if identity is None:
            raise ApiHttpError(401, "CONSOLE_TOKEN_INVALID", "令牌无效或已过期，请重新登录")

        if identity.role not in allowed:
            raise ApiHttpError(403, "CONSOLE_FORBIDDEN", "当前角色无权访问该功能")
        return identity

    return dependency
