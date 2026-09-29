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

# .env 与它的备份里是全部密钥（LLM key、向量库 key、后台口令），
# 默认 umask 落盘是 644，同机其它用户可读。收紧到只有属主可读。
chmod 600 backend/.env
chmod 600 backend/.env.bak.* 2>/dev/null || true

# 补齐后台口令。这一步不能省：上面的逻辑是「.env 存在就不动它」，
# 所以新加的 CONSOLE_* 永远不会自己出现在服务器上——功能上线了却登不进去。
# 只在缺失时生成，已配过的绝不覆盖（否则每次部署都把口令换掉，在线客服会被踢下线）。
#
# 三个键**都要探**：只看 CONSOLE_TOKEN_SECRET 的话，历史 .env 里留着密钥、
# 两个口令却是空的，这里会判定"已配置"而不生成，结果是后台永远 503。
if ! grep -q '^CONSOLE_TOKEN_SECRET=.\+' backend/.env \
   || ! grep -q '^CONSOLE_AGENT_PASSWORD=.\+' backend/.env \
   || ! grep -q '^CONSOLE_ADMIN_PASSWORD=.\+' backend/.env; then
    if ! command -v openssl >/dev/null 2>&1; then
        echo "❌ 缺少 openssl，无法生成后台口令；后台将保持关闭（受保护接口返回 503）"
        exit 1
    fi
    # 口令用 openssl 而不是 $RANDOM：后者只有 15 位、同秒内可预测
    _agent_pwd="$(openssl rand -base64 12 | tr -d '/+=' | cut -c1-14)"
    _admin_pwd="$(openssl rand -base64 12 | tr -d '/+=' | cut -c1-14)"
    _secret="$(openssl rand -hex 32)"

    # 生成失败必须当场停：管道会把 openssl 的失败码吃掉（取的是 cut 的退出码），
    # 悄悄写出一个空口令的话，后台会变成「部署成功但登不进去」，
    # 而且要等下一次部署才会重试
    for _v in "$_agent_pwd" "$_admin_pwd" "$_secret"; do
        if [ ${#_v} -lt 12 ]; then
            echo "❌ 口令生成异常（长度 ${#_v}），已中止。请检查 openssl 是否可用。"
            exit 1
        fi
    done

    {
        echo ""
        echo "# 后台口令（本次部署自动生成）"
        echo "CONSOLE_AGENT_PASSWORD=${_agent_pwd}"
        echo "CONSOLE_ADMIN_PASSWORD=${_admin_pwd}"
        echo "CONSOLE_TOKEN_SECRET=${_secret}"
        echo "CONSOLE_TOKEN_TTL_MINUTES=720"
    } >> backend/.env
    chmod 600 backend/.env

    # 口令**不往标准输出打**。本脚本由 GitHub Actions 通过 SSH 执行，
    # stdout 会回流进 Actions 运行日志——那是持久化、整个仓库可见的地方，
    # 等于把后台口令贴进 CI 日志。这里只落一份属主可读的文件。
    _cred="$HOME/.veyacare-console-credentials.txt"
    {
        echo "VeyaCare 后台口令（$(date '+%Y-%m-%d %H:%M:%S') 由部署脚本生成）"
        echo ""
        echo "客服工作台  CONSOLE_AGENT_PASSWORD = ${_agent_pwd}"
        echo "管理看板    CONSOLE_ADMIN_PASSWORD = ${_admin_pwd}"
        echo ""
        echo "登录地址    https://cs.veyawork.work/login"
        echo "这份文件由部署脚本生成，读完后请保存到密码管理器并删除它。"
    } > "$_cred"
    chmod 600 "$_cred"

    echo ""
    echo "后台口令已生成，写入：$_cred（仅属主可读）"
    echo "  ⚠️  口令刻意不打印到部署日志——部署日志是持久化且仓库可见的。"
    echo "     请 SSH 到服务器执行下面这条读取，存进密码管理器后删除该文件："
    echo "       cat $_cred && rm $_cred"
    echo ""
else
    echo "后台口令已配置，保持不变"
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
