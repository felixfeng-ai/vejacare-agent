"""后台登录：口令换令牌。

只做两件事——把口令换成令牌、把令牌换回身份。没有注册、没有找回密码、
没有用户列表：口令配在环境变量里（见 config.py 的 CONSOLE_* 四项），
改口令 = 改 .env 重启。这是共享口令方案的必然结果，也是它换来的简洁。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request

from app.config import get_settings
from app.schemas import ConsoleLoginRequest, ok
from app.security import (
    ROLE_ADMIN,
    ROLE_AGENT,
    ROLE_LABELS,
    ApiHttpError,
    Identity,
    clear_login_failures,
    issue_token,
    login_locked_for,
    match_password,
    record_login_failure,
    require_role,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/console", tags=["console"])


@router.post("/login")
async def login(request: Request, payload: ConsoleLoginRequest) -> dict:
    settings = get_settings()
    if not settings.console_enabled:
        raise ApiHttpError(503, "CONSOLE_DISABLED", "后台未配置访问口令，该功能未启用")

    locked = login_locked_for(request)
    if locked > 0:
        raise ApiHttpError(
            429, "CONSOLE_LOGIN_LOCKED", f"登录失败次数过多，请 {locked} 秒后再试"
        )

    role = match_password(payload.password)
    if role is None:
        record_login_failure(request)
        # 不区分「口令为空」「口令不对」「角色不存在」，统一一句：区分开就等于
        # 告诉试探者"这个口令前缀是对的，只是后面不对"，把暴力破解的空间砍掉一大截
        raise ApiHttpError(401, "CONSOLE_BAD_PASSWORD", "访问口令不正确")

    clear_login_failures(request)
    name = payload.name.strip() or ROLE_LABELS[role]
    token, expires_at = issue_token(role, name)
    logger.info("后台登录成功：%s（%s）", name, ROLE_LABELS[role])
    return ok(
        {
            "token": token,
            "role": role,
            "name": name,
            "expires_at": expires_at,
        }
    )


@router.get("/me")
async def me(identity: Identity = Depends(require_role(ROLE_AGENT, ROLE_ADMIN))) -> dict:
    """拿令牌换身份。前端刷新页面后用它在本地恢复登录态，不必再问一次口令。"""
    return ok(
        {
            "role": identity.role,
            "name": identity.name,
            "expires_at": identity.expires_at,
        }
    )
