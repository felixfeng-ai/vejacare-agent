"""配置解析的回归测试。

这里钉的是一个「照文档操作反而起不来」的坑：README 和 docker-compose 都要求
先 `cp .env.example .env`，而 .env.example 里给可选配置留了空值
（EMBEDDING_DIM= 留空 = 自动探测）。pydantic 会拿这个空字符串去解析 int，
直接抛 ValidationError，于是文档推荐的第一步就把服务弄挂了。

本地一直没暴露，是因为本地从来没真的建过 .env —— 走的是字段默认值。
"""

from __future__ import annotations

from app.config import BACKEND_DIR, Settings


def test_shipped_env_example_parses() -> None:
    """直接拿仓库里那份 .env.example 解析，必须成功。

    这是最贴近真实故障现场的写法：不是构造一个「带空值的配置」，而是用用户
    真正会复制的那份文件。.env.example 以后再加空值项，这条会立刻红。
    """
    example = BACKEND_DIR / ".env.example"
    assert example.exists(), ".env.example 是文档要求用户复制的文件，不能缺"

    settings = Settings(_env_file=example)

    # 留空 = 自动探测，落到默认值 0，而不是解析失败
    assert settings.embedding_dim == 0


def test_blank_numeric_env_falls_back_to_default(tmp_path) -> None:
    """数值项留空时回落到字段默认值，而不是抛 ValidationError。"""
    env = tmp_path / ".env"
    env.write_text(
        "EMBEDDING_DIM=\n"
        "RETRIEVE_TOP_K=\n"
        "RERANK_TOP_N=\n"
        "CHUNK_SIZE=\n",
        encoding="utf-8",
    )

    settings = Settings(_env_file=env)

    assert settings.embedding_dim == 0
    assert settings.retrieve_top_k == 20
    assert settings.rerank_top_n == 5
    assert settings.chunk_size == 420


def test_blank_string_env_stays_blank(tmp_path) -> None:
    """字符串项留空仍然是空串——空串本身就是合法值，不能拿默认值顶掉。

    llm_api_key="" 的语义是「没配 key」，与「回落到默认值」是两码事。
    """
    env = tmp_path / ".env"
    env.write_text("LLM_API_KEY=\nLLM_CLASSIFIER_MODEL=\n", encoding="utf-8")

    settings = Settings(_env_file=env)

    assert settings.llm_api_key == ""
    assert settings.llm_classifier_model == ""
    # 留空的分类模型要能正确回落到主模型
    assert settings.classifier_model == settings.llm_model


def test_explicit_values_still_win(tmp_path) -> None:
    """兜底逻辑不能把用户显式写的值也吃掉。"""
    env = tmp_path / ".env"
    env.write_text("EMBEDDING_DIM=1024\nRERANK_TOP_N=3\n", encoding="utf-8")

    settings = Settings(_env_file=env)

    assert settings.embedding_dim == 1024
    assert settings.rerank_top_n == 3
