"""精排层的回归测试。

`RERANK_PROVIDER=none` 过去是真的「不精排」——直接 `docs[:top_n]` 一刀切。
代价是 20 条候选里谁进前 5 完全由 RRF 名次决定，而 RRF 的 `1/(k+rank)` 在 k=60 时
被刻意压得很平：实测同一批候选里第 1 名与第 6 名只差 6%，截断点近乎随机，
答案排在第 6 就被无声丢掉——检索「成功」返回 5 条，只是没有一条写着答案。

第一个用例就是钉这个 bug 的，修之前它必然红。
"""

from __future__ import annotations

import pytest

from app.rag.rerank import LexicalReranker, get_reranker

pytestmark = pytest.mark.asyncio

#: 「包裹寄到德国一般几天？」的答案在 logistics.md 的时效表里。
#: 修复前这个 chunk 排第 6，正好被 `docs[:5]` 切掉。
GERMANY_FACT = "8–14 个工作日"


# ---------------------------------------------------------------- 端到端回归


async def test_answer_survives_the_top_n_cut():
    """召回里明明有这条，就不能让它死在第 5 名之后的截断上。"""
    from app.rag.hybrid import get_retriever

    docs = await get_retriever().retrieve(question="包裹寄到德国一般几天？", intent="logistics")

    assert docs, "检索不该为空"
    haystack = "\n".join(d["text"] for d in docs)
    assert GERMANY_FACT in haystack, (
        f"写道德国的时效的片段被挤出了前 {len(docs)} 名：{[d['id'] for d in docs]}"
    )


async def test_intent_expansion_does_not_outrank_the_question_itself():
    """意图扩展词只该用来扩大召回，不该拿来排序。

    `物流 时效 轨迹 清关 派送` 这串泛化词会让「恰好含这些词」的片段盖过真正回答
    问题的片段：「派送失败与二次派送」只因含「派送」二字就排到第 2，而写着德国时效的
    那条整个掉出前 5。修好之后它必须排在「派送失败」前面。
    """
    from app.rag.hybrid import get_retriever

    docs = await get_retriever().retrieve(question="包裹寄到德国一般几天？", intent="logistics")

    answer = next((i for i, d in enumerate(docs, start=1) if GERMANY_FACT in d["text"]), None)
    # 只靠泛化扩展词「派送」上榜的对照片段
    expansion_only = next(
        (i for i, d in enumerate(docs, start=1) if "派送失败与二次派送" in d["text"]), None
    )

    assert answer is not None, "答案片段不在前几名"
    assert expansion_only is not None, "对照片段本身该被召回（它含扩展词「派送」）"
    assert answer < expansion_only, (
        f"答案排第 {answer}、只含泛化扩展词的片段排第 {expansion_only}——排序还在被扩展词带跑"
    )


# ---------------------------------------------------------------- 精排本身


def _doc(chunk, score: float) -> dict:
    """把真实的 chunk 包成一条候选，只改 RRF 分数。

    词面分是拿 chunk id 去 BM25 索引里查的，所以这里必须用**真实入库过的** chunk，
    自己捏一个 id 会查不到分数、词面信号恒为 0，测出来的就不是精排逻辑了。
    """
    return {
        "id": chunk.id,
        "text": chunk.text,
        "source": chunk.source,
        "title": chunk.title,
        "snippet": "",
        "score": score,
    }


def _chunk_with(needle: str, *, exclude: str = ""):
    from app.rag.store import ChunkRepository

    repo = ChunkRepository()
    if len(repo) == 0:
        repo.load()
    # `exclude` 为空串时要跳过这个条件：空串是任何字符串的子串，
    # 直接写 `exclude not in c.text` 会恒为 False，把候选全滤光。
    return next(
        c for c in repo.all() if needle in c.text and (not exclude or exclude not in c.text)
    )


async def test_lexically_matching_doc_is_promoted():
    """RRF 序在后、但字面命中查询词的候选，应该被提上来。

    对照片段由数据自己挑（词面分为 0 的那条），不手挑——手挑的话
    「吊牌要留着吗」里的 `要/留/着/吗` 都是常用字，随便一篇文档都可能蹭到分。
    """
    from app.rag.bm25 import get_bm25_index
    from app.rag.store import ChunkRepository

    repo = ChunkRepository()
    if len(repo) == 0:
        repo.load()

    query = "吊牌"
    lexical = get_bm25_index().score_map(query)
    target = max(repo.all(), key=lambda c: lexical.get(c.id, 0.0))
    other = next(c for c in repo.all() if lexical.get(c.id, 0.0) == 0.0)

    assert lexical[target.id] > 0, "语料里该有一条真正命中「吊牌」"

    docs = [_doc(other, 0.0300), _doc(target, 0.0290)]  # RRF 把无关的排在前面
    # top_n 必须小于候选数：候选数 ≤ top_n 时精排直接原样返回，测不到排序逻辑
    out = await LexicalReranker().rerank(query, docs, 1)

    assert [d["id"] for d in out] == [target.id], "字面命中查询词的片段没有被提上来"


async def test_falls_back_to_fused_order_when_nothing_matches():
    """查询词在候选里一个都找不到时，不许乱排——保持上游 RRF 序。

    查询串必须是**纯 ASCII 乱码**：写成「语料里没有的词」这种中文是没有用的，
    里面每个字（有、的、词…）都会在中文语料里命中，根本构造不出零重叠。
    """
    from app.rag.bm25 import get_bm25_index

    a = _chunk_with("关税")
    b = _chunk_with("胸围", exclude="关税")
    docs = [_doc(a, 0.0300), _doc(b, 0.0290)]

    query = "zzzzqqqq"
    lexical = get_bm25_index().score_map(query)
    assert lexical.get(a.id, 0.0) == 0.0 and lexical.get(b.id, 0.0) == 0.0, (
        "前提不成立：这条查询和候选有词面重叠，测不出回退分支"
    )

    out = await LexicalReranker().rerank(query, docs, 1)

    assert [d["id"] for d in out] == [a.id]


async def test_short_candidate_list_is_returned_untouched():
    a = _chunk_with("关税")
    docs = [_doc(a, 0.02)]
    out = await LexicalReranker().rerank("关税", docs, 5)
    assert [d["id"] for d in out] == [a.id]


async def test_empty_input_is_safe():
    assert await LexicalReranker().rerank("随便", [], 5) == []


async def test_ranking_is_deterministic():
    """排序必须可复现，否则评测数字每次跑都不一样，也没法进 CI。"""
    docs = [
        _doc(_chunk_with("吊牌"), 0.0300),
        _doc(_chunk_with("退货窗口", exclude="吊牌"), 0.0300),  # 与上一条 RRF 同分
        _doc(_chunk_with("胸围", exclude="吊牌"), 0.0295),
    ]
    # top_n=2 < 候选数 3，保证真的走了排序而不是原样返回
    first = [d["id"] for d in await LexicalReranker().rerank("吊牌", docs, 2)]
    assert len(first) == 2
    for _ in range(5):
        again = [d["id"] for d in await LexicalReranker().rerank("吊牌", docs, 2)]
        assert again == first


async def test_default_provider_uses_the_lexical_reranker():
    """conftest 把 RERANK_PROVIDER 设成 none，它必须落到会真正排序的实现上。"""
    assert isinstance(get_reranker(), LexicalReranker)
