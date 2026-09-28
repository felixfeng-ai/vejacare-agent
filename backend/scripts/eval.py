"""RAG 检索离线评测。

    python -m scripts.eval                      # 跑全量，打印报告
    python -m scripts.eval --verbose            # 再打印每条明细
    python -m scripts.eval --min-hit-rate 0.9   # 回归门禁：低于阈值以非 0 退出
    python -m scripts.eval --json report.json   # 落一份机器可读的结果

评测集在 `evals/retrieval_set.jsonl`，100 条，覆盖 8 篇有效文档。
每条给出：问题、标注意图、应该命中的文档、答案里必须出现的关键事实。

**为什么不测「回答是否正确」**：离线跑评测时用的是 mock 模型，
回答是关键词模板拼的，测它只是在测模板本身，没有意义。
所以这里只测检索这一段——它不依赖任何外部 API，结果确定、可回归，
而且检索错了后面全错，是整条链路里最值得单测的一环。

两个口径刻意分开算：
- 检索指标用**标注意图**，衡量检索本身好不好
- 意图指标用分类器输出对比标注，衡量分类好不好
合在一起算的话，分类错了会污染检索得分，看不出该优化哪一段。

当前基线（`RERANK_PROVIDER=none`、`EMBEDDING_PROVIDER=hashing`、召回 20 → 精排 5）：

    Hit@5 100.0%   MRR 0.9567   要点覆盖 98.0%   完全通过 98.0%
    意图分类 90.0%   失效文档污染 0

**意图分类 90% 是这套关键词桩的上限，剩下的错不是「关键词没覆盖到」**：
把 10 条错例逐条看一遍，每一条都真的横跨两个意图——「我拒收包裹的话关税退不退」
既是改单也是关税，「信用卡退款是不是特别慢」既是支付也是退款。单标签分类体系
碰到这类问题本身就没有正确答案，继续堆关键词只会把错误从一类搬到另一类。

**残留 2 条未通过**（`ev-023 退货需要商品保持什么状态` 缺「吊牌」、
`ev-088 已经发货的订单还能取消吗` 缺「不接受取消」）——两条同源：
目标文档命中了，但装着答案的那个 chunk 没挤进 5 个名额（8 个 chunk 的文档只给 5 个位置）。
实测放宽名额的收益很薄：

    top_n=5   完全通过 98.0%   平均注入  946 字
    top_n=7   完全通过 99.0%   平均注入 1311 字
    top_n=10  完全通过 100.0%  平均注入 1823 字

**为了把评测凑到 100% 而把 top_n 调到 10 是不划算的**：上下文接近翻倍换 2 个点，
而且多注入的片段对别的查询多半是噪声。要真正解决得靠 chunk 级精排（交叉编码器），
不是靠多给几个名额。所以这里保持 5，把结论记在案。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

# 允许 `python scripts/eval.py` 和 `python -m scripts.eval` 两种跑法
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.rag.hybrid import get_retriever  # noqa: E402
from app.utils import setup_console_encoding  # noqa: E402

setup_console_encoding()

EVAL_SET = Path(__file__).resolve().parent.parent / "evals" / "retrieval_set.jsonl"

#: 入库时被 status: deprecated 过滤掉的文档。它永远不该出现在任何检索结果里。
DEPRECATED_SOURCE = "_deprecated-shipping-2025.md"


@dataclass
class CaseResult:
    id: str
    question: str
    category: str
    expect_sources: list[str]
    intent_expected: str
    intent_got: str
    sources_got: list[str]
    hit_rank: int | None          # 首个正确来源的排名，1 起；None 表示没命中
    missing_keywords: list[str] = field(default_factory=list)
    polluted: bool = False        # 结果里混进了失效文档

    @property
    def source_hit(self) -> bool:
        return self.hit_rank is not None

    @property
    def keyword_hit(self) -> bool:
        return not self.missing_keywords

    @property
    def passed(self) -> bool:
        """来源命中了，且答案要点都在检索到的文本里。"""
        return self.source_hit and self.keyword_hit

    @property
    def intent_ok(self) -> bool:
        return self.intent_got == self.intent_expected


def load_cases(path: Path = EVAL_SET) -> list[dict]:
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


async def run_case(case: dict) -> CaseResult:
    from app.llm_mock import classify_intent

    retriever = get_retriever()
    # 用标注意图检索：把分类误差隔离在检索指标之外
    docs = await retriever.retrieve(question=case["question"], intent=case["intent"])

    sources = [str(d.get("source", "")) for d in docs]

    hit_rank = next(
        (i for i, s in enumerate(sources, start=1) if s in case["expect_sources"]), None
    )

    # 关键词在「命中的那些片段」里找，而不是把整篇文档拿来匹配
    haystack = "\n".join(str(d.get("text", "")) for d in docs)
    missing = [k for k in case["expect_keywords"] if k not in haystack]

    return CaseResult(
        id=case["id"],
        question=case["question"],
        category=case["category"],
        expect_sources=case["expect_sources"],
        intent_expected=case["intent"],
        intent_got=str(classify_intent(case["question"]).get("intent", "")),
        sources_got=sources,
        hit_rank=hit_rank,
        missing_keywords=missing,
        polluted=DEPRECATED_SOURCE in sources,
    )


# ---------------------------------------------------------------- 汇总


def summarize(results: list[CaseResult]) -> dict:
    total = len(results)
    if not total:
        return {}

    hits = [r for r in results if r.source_hit]
    full = [r for r in results if r.passed]

    # MRR 只在命中的样本上取倒数排名，未命中的按 0 计入分母
    mrr = sum(1.0 / r.hit_rank for r in hits if r.hit_rank) / total

    keyword_total = sum(len(r.missing_keywords) + 1 for r in results)  # 每条至少 1 个要点
    keyword_missing = sum(len(r.missing_keywords) for r in results)

    return {
        "total": total,
        "hit_rate": round(len(hits) / total, 4),
        "mrr": round(mrr, 4),
        "keyword_coverage": round(1 - keyword_missing / keyword_total, 4),
        "full_pass_rate": round(len(full) / total, 4),
        "intent_accuracy": round(sum(1 for r in results if r.intent_ok) / total, 4),
        "pollution_count": sum(1 for r in results if r.polluted),
    }


def group_by(results: list[CaseResult], key) -> dict[str, dict]:
    buckets: dict[str, list[CaseResult]] = defaultdict(list)
    for r in results:
        buckets[key(r)].append(r)
    return dict(sorted(buckets.items(), key=lambda kv: -len(kv[1])))


# ---------------------------------------------------------------- 输出


def print_report(results: list[CaseResult], summary: dict, *, verbose: bool, top_k: int) -> None:
    bar = "─" * 68
    print(bar)
    print(f"  检索评测　{summary['total']} 条　（retrieve_top_k={top_k}）")
    print(bar)
    print(f"  来源命中率 Hit@k        {summary['hit_rate']:.1%}")
    print(f"  首个正确来源 MRR        {summary['mrr']:.4f}")
    print(f"  答案要点覆盖率          {summary['keyword_coverage']:.1%}")
    print(f"  完全通过（来源+要点）    {summary['full_pass_rate']:.1%}")
    print(f"  意图分类准确率          {summary['intent_accuracy']:.1%}")
    print(f"  失效文档污染            {summary['pollution_count']} 次", end="")
    print("　✅" if summary["pollution_count"] == 0 else "　❌ 知识库版本治理失效！")

    print()
    print("按文档：")
    for doc, rows in group_by(results, lambda r: r.expect_sources[0]).items():
        s = summarize(rows)
        flag = "" if s["hit_rate"] >= 0.9 else "  ← 偏低"
        print(
            f"  {doc:26s} {len(rows):>3} 条　命中 {s['hit_rate']:>6.1%}"
            f"　要点 {s['keyword_coverage']:>6.1%}{flag}"
        )

    failed = [r for r in results if not r.passed]
    if failed:
        print()
        print(f"未通过 {len(failed)} 条：")
        for r in failed:
            why = []
            if not r.source_hit:
                why.append(f"没检索到 {r.expect_sources[0]}（实际拿到 {r.sources_got[:2]}）")
            if r.missing_keywords:
                why.append(f"缺要点 {r.missing_keywords}")
            print(f"  [{r.id}] {r.question}")
            print(f"        {'；'.join(why)}")

    if verbose:
        print()
        print("全部明细：")
        for r in results:
            mark = "✅" if r.passed else "❌"
            rank = f"rank {r.hit_rank}" if r.hit_rank else "miss"
            intent = "" if r.intent_ok else f"  意图 {r.intent_expected}→{r.intent_got}"
            print(f"  {mark} [{r.id}] {rank:>8s}  {r.question}{intent}")

    print(bar)


async def main() -> int:
    parser = argparse.ArgumentParser(description="VeyaCare 检索离线评测")
    parser.add_argument("--set", type=Path, default=EVAL_SET, help="评测集路径")
    parser.add_argument("--verbose", action="store_true", help="打印每条明细")
    parser.add_argument("--json", type=Path, help="把结果写成 JSON")
    parser.add_argument(
        "--min-hit-rate", type=float, default=0.0, help="命中率低于此值则以非 0 退出（CI 门禁）"
    )
    parser.add_argument("--min-keyword-coverage", type=float, default=0.0, help="要点覆盖率门禁")
    args = parser.parse_args()

    cases = load_cases(args.set)
    if not cases:
        print(f"评测集为空：{args.set}")
        return 1

    # 索引没建的话检索必然全空，先给一句人话提示，别让人对着 0% 发呆
    from app.rag.ingest import warmup

    stats = await warmup()
    if stats["chunks"] == 0:
        print("知识库索引为空，请先执行：python -m scripts.ingest")
        return 1

    from app.config import get_settings

    results = [await run_case(c) for c in cases]
    summary = summarize(results)
    print_report(results, summary, verbose=args.verbose, top_k=get_settings().retrieve_top_k)

    if args.json:
        args.json.write_text(
            json.dumps(
                {
                    "summary": summary,
                    "cases": [r.__dict__ for r in results],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"结果已写入 {args.json}")

    failed_gates = []
    if summary["hit_rate"] < args.min_hit_rate:
        failed_gates.append(f"命中率 {summary['hit_rate']:.1%} < {args.min_hit_rate:.1%}")
    if summary["keyword_coverage"] < args.min_keyword_coverage:
        failed_gates.append(
            f"要点覆盖率 {summary['keyword_coverage']:.1%} < {args.min_keyword_coverage:.1%}"
        )
    if summary["pollution_count"]:
        failed_gates.append(f"失效文档被检索到 {summary['pollution_count']} 次")

    if failed_gates:
        print("门禁未通过：" + "；".join(failed_gates))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
