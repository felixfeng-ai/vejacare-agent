"""配置层的后台项：默认值、模板可加载性、密钥长度下限。

这些用例的存在理由是一次真实的线上失败。`console_token_secret` 最初写成
`Field(default="", min_length=16)`，注释里还写着「空串是合法写法，校验器不校验默认值」。
**这个前提是错的**：pydantic-settings 与 pydantic 不同，默认值也要过一遍校验，
于是默认值 "" 撞上了自己的 min_length，`Settings()` 直接抛 ValidationError。

后果是「没配后台」这个最该能跑的状态反而起不来，而它恰好是三条路径的共同起点：
README 的 `cp .env.example .env`、CI 的 Docker 冒烟、部署脚本的首次部署。
本地从来没人建过 .env，所以单测与本地启动全程绿灯，直到推上去才炸。

所以这里守的不是「某个字段的值」，是**配置层必须能在「什么都没配」的状态下加载**。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import BACKEND_DIR, MIN_SECRET_LENGTH, Settings

CONSOLE_ENV_KEYS = (
    "CONSOLE_AGENT_PASSWORD",
    "CONSOLE_ADMIN_PASSWORD",
    "CONSOLE_TOKEN_SECRET",
    "CONSOLE_TOKEN_TTL_MINUTES",
)


def _clear_console_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """抹掉 conftest 为鉴权测试铺的环境变量，模拟「全新克隆、什么都没配」。"""
    for key in CONSOLE_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_loads_without_any_console_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """什么都没配也要能加载，并且后台是关闭的。

    这是上面那次失败的直接回归守卫：这一行原来会抛 ValidationError。
    """
    _clear_console_env(monkeypatch)
    settings = Settings(_env_file=None)

    assert settings.console_token_secret == ""
    assert settings.console_enabled is False


def test_shipped_env_template_is_loadable(monkeypatch: pytest.MonkeyPatch) -> None:
    """`.env.example` 本身必须能直接当 `.env` 用。

    模板里后台三项是刻意留空的（不留任何默认口令），而留空恰恰是上面那个 bug 的触发条件。
    这条用例把「模板抄下来就能跑」这件事钉住——否则坏的是文档承诺的零配置路线，
    而不是某个具体功能，很容易在改配置时被顺手改坏。
    """
    _clear_console_env(monkeypatch)
    settings = Settings(_env_file=BACKEND_DIR / ".env.example")

    assert settings.console_enabled is False
    # 顺带确认模板真的没偷偷塞口令进去
    assert settings.console_agent_password == ""
    assert settings.console_admin_password == ""


@pytest.mark.parametrize("secret", ["", "短", "x" * (MIN_SECRET_LENGTH - 1)])
def test_secret_may_be_blank_but_not_short(secret: str) -> None:
    """留空是「关闭后台」，不是误配；但配了就必须够长。"""
    if secret == "":
        assert Settings(console_token_secret=secret).console_token_secret == ""
        return
    with pytest.raises(ValidationError):
        Settings(console_token_secret=secret)


def test_secret_at_the_limit_is_accepted() -> None:
    """边界值本身是合法的——下限是「至少」而不是「大于」。"""
    secret = "x" * MIN_SECRET_LENGTH
    assert Settings(console_token_secret=secret).console_token_secret == secret


def test_enabled_needs_both_password_and_secret() -> None:
    """只有口令或只有密钥都不算启用：缺密钥签不出令牌，缺口令进不来人。

    三项都显式传：conftest 为鉴权测试铺了 CONSOLE_* 环境变量，少传一个就测不出
    「缺这一项」——显式入参优先级高于环境变量，所以传空串才算真的缺。
    """
    def build(password: str, secret: str) -> Settings:
        return Settings(
            console_agent_password=password,
            console_admin_password="",
            console_token_secret=secret,
        )

    assert build(password="pwd", secret="").console_enabled is False
    assert build(password="", secret="s" * 32).console_enabled is False
    assert build(password="pwd", secret="s" * 32).console_enabled is True
