"""登录限流的分桶逻辑（纯单元，不走 HTTP）。

单独一个文件，因为这里验的是「计数表怎么分桶、怎么淘汰」这件事本身，
而不是接口契约。放在 test_console_auth.py 里会被那个文件的模块级 asyncio 标记
连累，而这些用例根本不需要事件循环。
"""

from __future__ import annotations

import time

from fastapi import Request

from app.security import MAX_LOGIN_FAILURES

def _fake_request(host: str, forwarded: str | None = None) -> Request:
    headers = [] if forwarded is None else [(b"x-forwarded-for", forwarded.encode())]
    return Request({"type": "http", "headers": headers, "client": (host, 12345)})


def test_client_key_ignores_forwarded_header():
    """限流键必须来自 ASGI 解析出的来源，不能来自请求头。

    这是一条回归守卫。生产的 nginx 用 `$proxy_add_x_forwarded_for`（**追加**语义），
    请求头会变成「客户端自带的假值, 真实 IP」——第一段由请求方随便填。
    之前按第一段计数时，攻击者每次换一个假 IP 就能让计数永远到不了阈值，
    共享口令可以无限穷举；反过来还能挑个受害者的 IP 去撞，把人家锁在门外。
    """
    from app.security import _client_key

    forged = _fake_request("203.0.113.9", forwarded="1.2.3.4, 203.0.113.9")
    assert _client_key(forged) == "203.0.113.9", "限流键取了请求头里可伪造的值"


def test_lock_is_per_source_not_global(monkeypatch):
    """锁定只针对撞口令的那个来源，不能把所有人一起关在门外。

    隔离逻辑按来源键分桶，所以这里直接换掉取键函数——测试要验的是「分桶对不对」，
    而不是「键从哪来」（那是上一条的事）。用请求头模拟两个来源正是上一版的错误，
    它让一条本应失败的断言变成了假绿。
    """
    from app import security

    keys = {"current": "attacker"}
    monkeypatch.setattr(security, "_client_key", lambda request: keys["current"])

    attacker = _fake_request("203.0.113.9")
    for _ in range(MAX_LOGIN_FAILURES):
        security.record_login_failure(attacker)

    assert security.login_locked_for(attacker) > 0, "撞了这么多次却没锁"

    keys["current"] = "innocent"
    assert security.login_locked_for(_fake_request("198.51.100.7")) == 0, (
        "一个来源被锁，把另一个来源也关了"
    )


def test_lock_expires(monkeypatch):
    """锁是临时的，到点要自己解开——否则一次误撞就把后台永久锁死。"""
    from app import security

    keys = {"current": "attacker"}
    monkeypatch.setattr(security, "_client_key", lambda request: keys["current"])

    request = _fake_request("203.0.113.9")
    for _ in range(MAX_LOGIN_FAILURES):
        security.record_login_failure(request)
    assert security.login_locked_for(request) > 0

    # 把记录里的解锁时刻拨到过去，模拟锁定期已过
    security._failures["attacker"].locked_until = time.time() - 1
    assert security.login_locked_for(request) == 0, "锁到点了却没解开"


def test_failure_table_has_a_capacity_limit(monkeypatch):
    """计数表不能无限增长。

    它是一张只增不减的字典，没有上限的话，海量来源撞口令就能把它撑成内存问题。
    """
    from app import security

    keys = {"current": "x"}
    monkeypatch.setattr(security, "_client_key", lambda request: keys["current"])
    monkeypatch.setattr(security, "MAX_TRACKED_SOURCES", 10)

    for i in range(50):
        keys["current"] = f"source-{i}"
        security.record_login_failure(_fake_request("203.0.113.9"))

    assert len(security._failures) <= 10, f"计数表涨到了 {len(security._failures)} 条"


