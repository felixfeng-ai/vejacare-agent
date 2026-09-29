"""满意度闭环的接口级测试。

覆盖 `POST /api/feedback` → 落库 → `GET /api/metrics/satisfaction` 可见这条完整回路。
此前这条回路一条测试都没有，而它恰恰是「接口报成功、数据其实没落库」最容易藏身的地方：
写接口用的是 `Depends(get_db)`，而 `get_db` 与 `session_scope` 的提交语义并不一致。

断言一律用「前后差值」而不是绝对值：整个测试会话共用一个数据库，
`satisfaction_metrics` 统计的是全表，写死数字会被其它测试的数据带偏。
"""

from __future__ import annotations

from httpx import AsyncClient

from app.db.repository import add_message, ensure_session
from app.db.session import session_scope


async def _make_session(session_id: str, *, intent: str = "") -> str:
    """建一个真实存在的会话，可选带上 intent。评价必须挂在存在的会话上。"""
    async with session_scope() as db:
        await ensure_session(db, session_id, first_message="我的包裹到哪了")
        if intent:
            await add_message(db, session_id=session_id, role="user", content="查物流", intent=intent)
    return session_id


async def _metrics(admin_client: AsyncClient) -> dict:
    # 看板只对管理者开放，读它必须带管理令牌；
    # 提交评价那条 /api/feedback 仍然是公开的，仍用匿名 client

    resp = await admin_client.get("/api/metrics/satisfaction")
    body = resp.json()
    assert body["success"] is True, body
    return body["data"]


# ---------------------------------------------------------------- 主回路


async def test_submitted_feedback_survives_and_shows_up_in_metrics(client: AsyncClient, admin_client: AsyncClient):
    """提交评价后，看板必须真的看到它。

    这是整个闭环的核心断言：接口返回的 feedback_id 不能是「写完就丢」。
    """
    session_id = await _make_session("sess-fb-persist")
    before = await _metrics(admin_client)

    resp = await client.post(
        "/api/feedback",
        json={"session_id": session_id, "message_id": "m-1", "rating": 5, "comment": "很快"},
    )
    body = resp.json()
    assert resp.status_code == 200, resp.text
    assert body["success"] is True, body
    assert body["data"]["feedback_id"], "必须返回落库后的 feedback_id"
    assert body["data"]["rating"] == 5

    # 换一个请求、换一个数据库会话去读——只有真提交了才读得到
    after = await _metrics(admin_client)
    assert after["rated_sessions"] == before["rated_sessions"] + 1, "评价没有落库"
    assert after["rating_distribution"]["5"] == before["rating_distribution"]["5"] + 1


async def test_low_rating_persists_too(client: AsyncClient, admin_client: AsyncClient):
    """低分走的是 `rating <= 2` 的告警分支，同样必须落库。

    这条分支额外记了一条 warning 日志，容易被写成提前 return 而漏掉落库。
    """
    session_id = await _make_session("sess-fb-low")
    before = await _metrics(admin_client)

    resp = await client.post(
        "/api/feedback",
        json={"session_id": session_id, "rating": 1, "comment": "答非所问"},
    )
    assert resp.json()["success"] is True

    after = await _metrics(admin_client)
    assert after["rated_sessions"] == before["rated_sessions"] + 1
    assert after["rating_distribution"]["1"] == before["rating_distribution"]["1"] + 1


async def test_avg_rating_reflects_new_score(client: AsyncClient, admin_client: AsyncClient):
    """均分要把新评价算进去，而不是停在旧值。"""
    session_id = await _make_session("sess-fb-avg")
    before = await _metrics(admin_client)

    resp = await client.post("/api/feedback", json={"session_id": session_id, "rating": 4})
    assert resp.json()["success"] is True
    after = await _metrics(admin_client)

    # 期望值用返回的原始数据自己算，不依赖其它测试留下的分布
    assert after["rated_sessions"] == before["rated_sessions"] + 1
    if before["avg_rating"] is not None and before["rated_sessions"]:
        prev_total = before["avg_rating"] * before["rated_sessions"]
        expected = round((prev_total + 4) / (before["rated_sessions"] + 1), 2)
        assert after["avg_rating"] == expected


async def test_feedback_records_session_intent(client: AsyncClient, admin_client: AsyncClient):
    """评价要带上会话当时的意图，否则 bad case 没法按意图归类。"""
    session_id = await _make_session("sess-fb-intent", intent="logistics")

    resp = await client.post("/api/feedback", json={"session_id": session_id, "rating": 3})
    assert resp.json()["success"] is True

    metrics = await _metrics(admin_client)
    intents = {row["intent"]: row["count"] for row in metrics["top_intents"]}
    assert "logistics" in intents, "会话意图没有进看板"


# ---------------------------------------------------------------- 边界与拒绝


async def test_feedback_for_unknown_session_is_rejected(client: AsyncClient, admin_client: AsyncClient):
    """挂到不存在的会话上必须拒绝，且不能污染看板。"""
    before = await _metrics(admin_client)

    resp = await client.post(
        "/api/feedback", json={"session_id": "sess-does-not-exist", "rating": 5}
    )
    body = resp.json()
    assert body["success"] is False, body
    assert body["error"]["code"] == "SESSION_NOT_FOUND"

    after = await _metrics(admin_client)
    assert after["rated_sessions"] == before["rated_sessions"], "被拒绝的评价不该留下记录"


async def test_rating_out_of_range_is_rejected(client: AsyncClient, admin_client: AsyncClient):
    """评分是 1–5，越界交给 Pydantic 拦在校验层（422），不要进业务代码。"""
    session_id = await _make_session("sess-fb-range")
    before = await _metrics(admin_client)

    for bad in (0, 6, -1):
        resp = await client.post("/api/feedback", json={"session_id": session_id, "rating": bad})
        assert resp.status_code == 422, f"rating={bad} 应该被拒，实际 {resp.status_code}"

    after = await _metrics(admin_client)
    assert after["rating_distribution"] == before["rating_distribution"], "越界评分不该落库"


async def test_comment_too_long_is_rejected(client: AsyncClient):
    """备注有 1000 字上限，超长同样在校验层拦掉。"""
    session_id = await _make_session("sess-fb-comment")
    resp = await client.post(
        "/api/feedback", json={"session_id": session_id, "rating": 5, "comment": "长" * 1001}
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------- 派生指标


async def test_auto_resolved_rate_matches_counts(admin_client: AsyncClient):
    """自动解决率 = 没转人工的会话占比，必须与同时返回的两个计数自洽。"""
    metrics = await _metrics(admin_client)

    total = metrics["total_sessions"]
    escalated = metrics["escalation_count"]
    expected = round((total - escalated) / total, 4) if total else 0.0
    assert metrics["auto_resolved_rate"] == expected
    assert metrics["escalation_rate"] == (round(escalated / total, 4) if total else 0.0)


async def test_metrics_shape_is_stable(admin_client: AsyncClient):
    """看板字段是前端契约的一部分，缺字段会让页面白屏。"""
    metrics = await _metrics(admin_client)

    for key in (
        "total_sessions",
        "rated_sessions",
        "avg_rating",
        "rating_distribution",
        "escalation_count",
        "escalation_rate",
        "top_intents",
        "auto_resolved_rate",
    ):
        assert key in metrics, f"缺少字段 {key}"

    # 分布必须五个档位齐全，前端直接按下标渲染
    assert set(metrics["rating_distribution"]) == {"1", "2", "3", "4", "5"}
    assert all(isinstance(v, int) for v in metrics["rating_distribution"].values())
