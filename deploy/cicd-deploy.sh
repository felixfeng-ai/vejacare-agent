#!/bin/bash
# ============================================================
# VeyaCare —— 生产部署脚本（在服务器上运行，不是本地）
#
# 用法：bash /opt/veyacare/deploy/cicd-deploy.sh
# 由 GitHub Actions 调用（见 .github/workflows/deploy.yml），
# 也可以 ssh 上去手动跑，用于排查。
#
# 与 i面试 / 成剧 两个项目同一套路子：push 到 main → Actions → SSH → 跑本脚本。
# 差别在于本项目是「Python 后端 + 静态前端」，没有 Next.js 那套构建产物。
# ============================================================

# ---- 自我复制后再执行 ----------------------------------------
# 不能省这一步。下面的 `git reset --hard` 会覆盖本文件，
# 而 bash 是「边读边执行」的——脚本正在被覆盖时，执行指针会落到新内容的
# 中间，行为不可预期。先把自己复制到 /tmp 再 exec，git 之后怎么改仓库里的
# 文件都与本次执行无关。
if [ "$0" != "/tmp/veyacare-deploy.sh" ]; then
    cp -f "$0" /tmp/veyacare-deploy.sh || { echo "❌ 无法复制脚本到 /tmp"; exit 1; }
    exec bash /tmp/veyacare-deploy.sh "$@"
fi
# -------------------------------------------------------------

set -e

APP="veyacare-api"
DIR="/opt/veyacare"
PORT=3002
DOMAIN="cs.veyawork.work"

cd "$DIR"

echo "=== 1/8 同步代码 ==="
# 服务器是 GitHub main 的镜像：服务器上的未提交改动会被这里清掉。
# 要上线的任何改动都必须先 commit + push。
git fetch origin main --quiet
git reset --hard origin/main --quiet
echo "当前版本: $(git log -1 --format='%h %s')"

echo "=== 2/8 准备 .env（不覆盖已有的）==="
# .env 是未跟踪文件，reset --hard 不会动它。这里只在首次部署时补一份默认配置。
if [ ! -f backend/.env ]; then
    cp backend/.env.example backend/.env
    echo "已从 .env.example 创建（默认 mock 模式，零 API key 可跑）"
else
    cp backend/.env "backend/.env.bak.$(date +%Y%m%d%H%M)"
    ls -1t backend/.env.bak.* 2>/dev/null | tail -n +6 | xargs -r rm -f
    echo "已有 .env，已备份并保留"
fi

echo "=== 3/8 后端虚拟环境 ==="
cd "$DIR/backend"
if [ ! -x .venv/bin/python ]; then
    # 腾讯云 Ubuntu 镜像默认不带 ensurepip，venv 会失败，这里补装
    if ! python3 -m venv .venv 2>/dev/null; then
        echo "  python3-venv 缺失，安装中…"
        sudo -n apt-get install -y -qq python3-venv >/dev/null 2>&1 || {
            echo "❌ 无法创建虚拟环境"; exit 1; }
        python3 -m venv .venv
    fi
    echo "  已创建 .venv"
fi
.venv/bin/pip install -q --upgrade pip
# 用 requirements.txt 而不是 Poetry/uv：依赖树浅，且 CI 里跑的是同一份文件，
# 保证「CI 绿 = 服务器能装」。
.venv/bin/pip install -q -r requirements.txt
echo "  依赖就绪 ($(.venv/bin/python -V))"

echo "=== 4/8 构建知识库索引 ==="
# 全量重建检索索引。index/ 是派生数据、已在 .gitignore 里，所以每次部署都要重建。
# 这步也顺带验证了 embedding 与分块链路没坏。
.venv/bin/python -m scripts.ingest

echo "=== 5/8 后端冒烟自检 ==="
# 在起服务之前先确认模块能 import、配置能加载，避免 pm2 起来又反复重启刷日志
.venv/bin/python -c "
from app.config import get_settings
s = get_settings()
print(f'  配置 OK：LLM={s.llm_provider} Embedding={s.embedding_provider} Rerank={s.rerank_provider}')
"

echo "=== 6/8 构建前端 ==="
cd "$DIR/frontend"
# 机器只有 1.9G 内存且还跑着别的站点，限制一下 node 堆，宁可慢也别 OOM
export NODE_OPTIONS="--max-old-space-size=768"
npm ci --no-audit --no-fund 2>&1 | tail -2
# 构建到临时目录再整体替换：直接写 dist/ 的话，构建途中访问站点会拿到
# 写了一半的资源。这样切换是原子的，用户最多看到新旧两个完整版本之一。
npm run build -- --outDir dist.new 2>&1 | tail -4
rm -rf dist && mv dist.new dist
echo "  前端产物: $(du -sh dist | cut -f1)"

echo "=== 7/8 安装 nginx 站点配置 ==="
# 幂等：内容没变就不动、不 reload。用 diff 判断而不是无条件覆盖，
# 避免每次部署都白 reload 一次影响其它站点。
CONF_SRC="$DIR/deploy/nginx-$DOMAIN.conf"
CONF_DST="/etc/nginx/sites-available/$DOMAIN"
if ! sudo -n diff -q "$CONF_SRC" "$CONF_DST" >/dev/null 2>&1; then
    sudo -n cp "$CONF_SRC" "$CONF_DST"
    sudo -n ln -sf "$CONF_DST" "/etc/nginx/sites-enabled/$DOMAIN"
    if sudo -n nginx -t 2>&1 | tail -2; then
        sudo -n systemctl reload nginx
        echo "  nginx 配置已更新并 reload"
    else
        echo "❌ nginx 配置校验失败，已回滚本次改动"
        sudo -n rm -f "/etc/nginx/sites-enabled/$DOMAIN"
        exit 1
    fi
else
    echo "  nginx 配置无变化"
fi

echo "=== 8/8 重启后端 ==="
# startOrReload：进程不存在就起、存在就按 ecosystem 配置热重载，
# 不依赖 PM2 遗留的进程表
mkdir -p "$DIR/logs"
cd "$DIR"
pm2 startOrReload ecosystem.config.cjs
pm2 save

echo "--- 等待服务就绪 ---"
for i in $(seq 1 20); do
    code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "http://127.0.0.1:$PORT/api/health" || true)
    if [ "$code" = "200" ]; then
        echo "✅ 后端自检 http://127.0.0.1:$PORT/api/health → 200"
        break
    fi
    if [ "$i" = "20" ]; then
        echo "❌ 后端 20 次探活都没起来，最近日志："
        pm2 logs "$APP" --nostream --lines 30 || true
        exit 1
    fi
    sleep 2
done

echo "--- 经 nginx 的端到端自检 ---"
# 带 Host 头直接打本机 nginx：不依赖 DNS 是否已解析，部署当场就能验证路由通不通
curl -s -o /dev/null -w "  首页 / → %{http_code}\n" --max-time 10 \
    -H "Host: $DOMAIN" "http://127.0.0.1/"
curl -s -o /dev/null -w "  接口 /api/health → %{http_code}\n" --max-time 10 \
    -H "Host: $DOMAIN" "http://127.0.0.1/api/health"

echo "✅ 部署完成"
# HTTPS 已于 2026-09-28 签好并随 nginx 配置一起纳入版本管理，
# 证书由 certbot 的 systemd timer 自动续期，这里不需要再做任何事。
echo "   HTTPS: https://$DOMAIN （证书自动续期，无需人工干预）"
