"""知识库分块。

分块策略按「结构优先、长度兜底」两级：
1. 先按 markdown 标题切成语义段（客服政策文档天然是一节一个主题）
2. 段太长再按段落切；段落还长就按句子滑窗切，带 overlap 防止答案被切断

每个 chunk 都带上 `标题 > 小节` 的路径前缀，让 embedding 和 BM25 都能吃到层级信息。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
#: 中英文句末标点，用于滑窗切分
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？；.!?;])\s*")


@dataclass
class Chunk:
    """一个入库单元。"""

    id: str
    text: str            # 带标题前缀的完整文本，用于 embedding 与展示
    title: str           # 所属小节标题
    source: str          # 来源文件名
    heading_path: str    # "文档标题 > 一级 > 二级"
    index: int           # 在该文档内的序号
    metadata: dict = field(default_factory=dict)

    def to_payload(self) -> dict:
        return {
            "id": self.id,
            "text": self.text,
            "title": self.title,
            "source": self.source,
            "heading_path": self.heading_path,
            "index": self.index,
            **self.metadata,
        }


def _split_sections(markdown: str) -> list[tuple[str, str, list[str]]]:
    """按标题切成 (标题, 层级路径, 正文行) 三元组。"""
    sections: list[tuple[str, str, list[str]]] = []
    stack: list[str] = []
    current_title = ""
    current_lines: list[str] = []

    def flush() -> None:
        if current_title or any(line.strip() for line in current_lines):
            sections.append((current_title, " > ".join(stack), list(current_lines)))

    for line in markdown.splitlines():
        match = _HEADING_RE.match(line)
        if not match:
            current_lines.append(line)
            continue

        flush()
        level = len(match.group(1))
        title = match.group(2).strip()

        # 同级或更高级标题出现时，弹出栈里更深的层级
        while len(stack) >= level:
            stack.pop()
        stack.append(title)

        current_title = title
        current_lines = []

    flush()
    return sections


def _split_long_text(text: str, size: int, overlap: int) -> list[str]:
    """按段落 → 句子做滑窗切分。"""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]

    pieces: list[str] = []
    buffer = ""
    for para in paragraphs:
        if len(para) > size:
            if buffer:
                pieces.append(buffer)
                buffer = ""
            pieces.extend(_sliding_window(para, size, overlap))
            continue

        candidate = f"{buffer}\n\n{para}" if buffer else para
        if len(candidate) <= size:
            buffer = candidate
        else:
            pieces.append(buffer)
            buffer = para

    if buffer:
        pieces.append(buffer)
    return pieces


def _sliding_window(text: str, size: int, overlap: int) -> list[str]:
    sentences = [s for s in _SENTENCE_SPLIT_RE.split(text) if s.strip()]
    windows: list[str] = []
    buffer = ""

    for sentence in sentences:
        if len(sentence) > size:
            # 单句就超长（例如没有标点的长表格行），硬切
            if buffer:
                windows.append(buffer)
                buffer = ""
            for i in range(0, len(sentence), size - overlap):
                windows.append(sentence[i : i + size])
            continue

        candidate = buffer + sentence
        if len(candidate) <= size:
            buffer = candidate
        else:
            windows.append(buffer)
            # 回带 overlap 个字符，避免答案正好被切在边界
            tail = buffer[-overlap:] if overlap else ""
            buffer = tail + sentence

    if buffer.strip():
        windows.append(buffer)
    return [w for w in windows if w.strip()]


def chunk_markdown(
    markdown: str,
    *,
    source: str,
    chunk_size: int = 420,
    chunk_overlap: int = 80,
    doc_title: str = "",
) -> list[Chunk]:
    """把一篇 markdown 切成 Chunk 列表。"""
    chunks: list[Chunk] = []
    counter = 0

    for title, heading_path, lines in _split_sections(markdown):
        body = "\n".join(lines).strip()
        if not body:
            # 标题下没有正文的，跳过（纯目录节点）
            continue

        path = heading_path or doc_title or source
        # 标题前缀：让「这一块讲什么」进入向量空间
        prefix = f"[{path}] "

        for piece in _split_long_text(body, chunk_size, chunk_overlap):
            text = prefix + piece
            chunks.append(
                Chunk(
                    id=f"{source}::{counter}",
                    text=text,
                    title=title or doc_title or source,
                    source=source,
                    heading_path=path,
                    index=counter,
                )
            )
            counter += 1

    return chunks


def load_documents(knowledge_dir: Path) -> list[Chunk]:
    """读取目录下所有 .md，切块后返回。"""
    if not knowledge_dir.exists():
        return []

    from app.config import get_settings

    settings = get_settings()
    all_chunks: list[Chunk] = []

    for path in sorted(knowledge_dir.glob("*.md")):
        content = path.read_text(encoding="utf-8")
        doc_title = path.stem
        for line in content.splitlines():
            m = _HEADING_RE.match(line)
            if m and len(m.group(1)) == 1:
                doc_title = m.group(2).strip()
                break

        all_chunks.extend(
            chunk_markdown(
                content,
                source=path.name,
                chunk_size=settings.chunk_size,
                chunk_overlap=settings.chunk_overlap,
                doc_title=doc_title,
            )
        )

    return all_chunks
