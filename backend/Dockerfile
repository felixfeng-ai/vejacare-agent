# VeyaCare 后端镜像
#
# 分两段：依赖单独一层，只要 requirements.txt 没变就不会重装——
# 改业务代码是秒级重建，改依赖才是分钟级。

FROM python:3.12-slim AS deps

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# 只拷依赖清单，让这层能被缓存住
COPY requirements.txt .
# 装到独立前缀，下一段整个搬走。刻意不写 PYTHONPATH=/install/lib/python3.12/...，
# 那种写法把解释器小版本写死在文件里，基础镜像一升级就静默失效。
RUN pip install --prefix=/install -r requirements.txt


FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    # 中文日志与知识库文档在 C locale 下会乱码，显式钉住 UTF-8
    PYTHONIOENCODING=utf-8 \
    PYTHONUTF8=1

WORKDIR /app

# 不在容器里跑 root：镜像被拿下时少一层提权面
RUN useradd --create-home --uid 10001 appuser

# /install 覆盖 /usr/local，bin 与 site-packages 自然并进去
COPY --from=deps /install /usr/local
COPY . .

# 运行期要写 SQLite 与检索索引。属主在这里给到，
# 下面 compose 把命名卷挂到 /app/data 时 Docker 会连属主一起继承过去，
# 否则卷是 root 的，非 root 进程起手就 Permission denied。
RUN mkdir -p /app/data && chown -R appuser:appuser /app

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4).status == 200 else 1)"

# 入库放在启动脚本里而不是构建时：索引依赖运行期的 embedding 配置，
# 构建时烘进去会把当时那套模型焊死在镜像里，之后换模型就对不上了。
# 重建本身是毫秒级（只有文本变了的 chunk 才重新调 embedding 接口）。
CMD ["sh", "-c", "python -m scripts.ingest && exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers"]
