"""知识库入库 CLI。

    python -m scripts.ingest              # 全量重建索引
    python -m scripts.ingest --stats      # 只看当前索引状态，不重建

每次重建都会打印「哪些文档变了」，配合 manifest.json 形成最简单的版本治理。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

# 允许 `python scripts/ingest.py` 和 `python -m scripts.ingest` 两种跑法
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import KNOWLEDGE_DIR, get_settings  # noqa: E402
from app.rag.ingest import ingest, read_manifest  # noqa: E402
from app.utils import setup_console_encoding  # noqa: E402

setup_console_encoding()


def _print_manifest() -> None:
    manifest = read_manifest()
    if not manifest:
        print("尚无入库记录，请先执行 python -m scripts.ingest")
        return

    report = manifest.get("report") or {}
    print(f"文档 {report.get('documents', 0)} 篇 / 分块 {report.get('chunks', 0)} 条")
    print(f"向量库：{report.get('vector_backend', '-')} / BM25：{report.get('bm25_size', 0)} 条")
    print("\n已入库文档：")
    for name in sorted((manifest.get("fingerprints") or {}).keys()):
        print(f"  - {name}")


async def main() -> int:
    parser = argparse.ArgumentParser(description="VeyaCare 知识库入库")
    parser.add_argument("--stats", action="store_true", help="只打印当前索引状态")
    parser.add_argument("--dir", type=Path, default=KNOWLEDGE_DIR, help="知识库目录")
    args = parser.parse_args()

    if args.stats:
        _print_manifest()
        return 0

    settings = get_settings()
    before = (read_manifest().get("fingerprints") or {})

    print(f"知识库目录：{args.dir}")
    print(f"分块参数：size={settings.chunk_size} overlap={settings.chunk_overlap}")
    print(f"向量库：{'Qdrant @ ' + settings.qdrant_url if settings.qdrant_url else '本地（未配 QDRANT_URL）'}")
    print("-" * 60)

    report = await ingest(knowledge_dir=args.dir, previous_fingerprints=before)

    print("-" * 60)
    print(report.summary())

    if report.changed_documents:
        print(f"\n本次变更的文档（{len(report.changed_documents)} 篇）：")
        for name in report.changed_documents:
            print(f"  ~ {name}")
    elif before:
        print("\n所有文档均无变更（向量全部命中缓存）")

    if report.skipped_deprecated:
        print(f"\n已跳过 {report.skipped_deprecated} 篇失效文档（status: deprecated）")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
