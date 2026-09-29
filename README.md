# VeyaCare — 跨境 AI 客服 Agent

面向跨境电商场景的 AI 客服 Agent。用 **LangGraph** 编排多轮对话：意图识别 → RAG 知识库检索 → 工具调用 → 生成回复 → 满意度评估，并支持 **Human-in-the-loop 转人工**（挂起 / 恢复，上下文不丢）。

业务场景对标 SHEIN 全球客服：物流跟踪、退换货、关税政策、尺码选择、支付失败、优惠券、订单修改。

---

## 目录

- [快速开始](#快速开始)
- [架构](#架构)
- [核心设计决策](#核心设计决策)
- [技术栈](#技术栈)
- [目录结构](#目录结构)
- [配置](#配置)
- [测试](#测试)
- [离线评测](#离线评测)
- [部署](#部署)
- [当前进度](#当前进度)

---

## 快速开始

**零配置起步**：默认 `LLM_PROVIDER=mock`，不需要任何 API key、不需要 Docker、不需要 Qdrant，就能跑通完整链路（mock 是确定性桩，不是模型，见下文）。

### 1. 后端

```bash
cd backend
python -m venv .venv
.venv/Scripts/activate          # Windows；macOS/Linux 用 source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env            # 默认就是 mock 模式，直接可用
python -m scripts.ingest        # 建知识库索引（必须，否则检索为空）
uvicorn app.main:app --reload   # http://127.0.0.1:8000
```

> `.env` 必须存为 **UTF-8**。Windows 默认 GBK 会让里面的中文注释乱码，严重时直接抛 `UnicodeDecodeError`。
>
> 索引产物在 `backend/data/index/`（已 gitignore），由 `data/knowledge/*.md` 生成。改了知识库文档就要重跑 `scripts.ingest`。

### 2. 前端

```bash
cd frontend
npm install
npm run dev                     # http://localhost:5173
```

开发态前端走相对路径 `/api/**`，由 Vite 代理转发到 `127.0.0.1:8000`（无 CORS 问题）。要指到别的后端就在 `frontend/.env` 里设 `VITE_API_BASE`。

### 3. 或者：Docker 一条命令

```bash
cp backend/.env.example backend/.env      # 必须先做，理由见下
docker compose up --build
# 前端 http://localhost:8080   后端 http://127.0.0.1:8000（只绑本机）
```

`.env` 是**必须**存在的：compose 用 `env_file` 把整个文件注入后端，这是唯一一份配置，本地开发和容器跑的是同一套。不想要这一步就直接改 `docker-compose.yml` 里的 `env_file` 段。

几个不那么显然的点：

- **前端不把后端地址烘进 JS**。生产态走相对路径 `/api/**`，由容器里的 nginx 反代到 `backend:8000`。注入 `VITE_API_BASE` 会让同一份产物换不了环境。
- **SSE 必须关掉 nginx 缓冲**（`proxy_buffering off`）。默认 nginx 会攒够一个 buffer 再发，流式打字机会变成「转圈半天然后整段蹦出来」。`proxy_read_timeout` 也放宽到 300s——逐字返回意味着连接长期静默，默认 60s 会把长回答掐断。
- **数据卷挂在 `/app/data`**。镜像里这个目录存在且属主是 `appuser`，Docker 初始化命名卷时会连属主一起继承；挂到新建的 `/data` 则归 root，非 root 进程起手就 `Permission denied`。
- **入库在容器启动时跑**，不在构建时。索引依赖运行期的 embedding 配置，构建时烘进去会把当时那套模型焊死在镜像里。

### 4. 接真实模型

编辑 `backend/.env`：

```ini
LLM_PROVIDER=deepseek
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_API_KEY=sk-你的key
LLM_MODEL=deepseek-chat

EMBEDDING_PROVIDER=dashscope
EMBEDDING_API_KEY=sk-你的key
EMBEDDING_MODEL=text-embedding-v4
```

改完要重跑 `python -m scripts.ingest` —— **换了 embedding 模型必须重建索引**，否则向量空间对不上。

想要生产级向量库就起 Qdrant 并填 `QDRANT_URL`；留空则自动降级为进程内本地向量库。

---

## 架构

```
                         START
                           │
                           ▼
                  ┌─────────────────┐
                  │ intent_classifier│  意图分类 + 槽位抽取（订单号/运单号/SKU…）
                  └────────┬────────┘
                           │
        ┌──────────────────┼──────────────────────┐
        │ human_agent      │ 需知识               │ 闲聊
        │                  ▼                      │
        │         ┌─────────────────┐             │
        │         │  kb_retriever   │ 混合检索     │
        │         └────────┬────────┘             │
        │                  │                      │
        │        ┌─────────┴─────────┐            │
        │        │ 需实时数据         │ 通用政策    │
        │        ▼                   │            │
        │  ┌───────────────┐         │            │
        │  │ tool_executor │         │            │
        │  └───────┬───────┘         │            │
        │          │                 │            │
        │          ▼                 ▼            ▼
        │      ┌──────────────────────────────────────┐
        │      │             responder                │  ← 唯一产出用户可见文本的节点
        │      └───────────────────┬──────────────────┘
        │                          ▼
        │                  ┌───────────────┐
        │                  │ turn_evaluator│  本轮是否解决 / 是否该转人工
        │                  └───────┬───────┘
        │                          │
        │      ┌───────────────────┼──────────────────┐
        │      │ 触发转人工         │ 已解决            │ 待用户补充
        │      ▼                   ▼                  ▼
        │ ┌──────────────────┐  ┌──────────┐        END
        └►│ escalation_notice│  │ feedback │
          └────────┬─────────┘  └────┬─────┘
                   ▼                 ▼
          ┌─────────────────┐      END
          │ escalation_wait │  interrupt() 挂起，等人工
          └────────┬────────┘
                   │ Command(resume={"reply": ...})
                   ▼
              responder（做收尾）
```

### 一次对话的时序

```
POST /api/chat/stream
  ├─ event: session     会话 id
  ├─ event: node        intent_classifier  正在理解您的问题
  ├─ event: intent      logistics / 物流跟踪 / {order_no: "SO..."}
  ├─ event: node        kb_retriever       正在检索知识库
  ├─ event: kb          [命中片段 + 分数]
  ├─ event: tool        query_logistics running
  ├─ event: tool        query_logistics done  {latest: "已到达美国洛杉矶分拨中心", eta: ...}
  ├─ event: token       "您的包裹"          ← 流式打字机
  ├─ event: token       "目前在美国洛杉矶分拨中心"
  └─ event: done        {message_id, intent, escalated}
```

完整事件契约（13 种 `type`、载荷字段、错误与重试语义）见 **[docs/API.md](docs/API.md)**。

---

## 核心设计决策

### 1. 防编造是**结构性**保证，不是靠 prompt 求模型

客服场景里编造物流状态和退款金额就是事故。所以：**LLM 只能看到 responder 节点明确喂给它的东西**（知识片段、工具结果、人工回复）。物流轨迹、订单金额、退款费用这些事实**没有别的来源**——工具没查到，prompt 里就没有，模型无从编起。

对应地，`responder` 的 prompt 里把硬约束写死：不许编造、资料里没有就说"帮您跟专项团队确认"、缺订单号就直接问用户要。

### 2. 转人工拆成两个节点，是因为 `interrupt()` 会丢状态

LangGraph 的 `interrupt()` 抛出后，**该节点已做的 state 变更全部丢弃**，resume 时节点从头重跑。所以：

- `escalation_notice` — 建工单、发事件、产出「已为您转接」气泡。**正常返回**，消息被 checkpoint 提交，用户看得到。
- `escalation_wait` — 只调 `interrupt()` 挂起。节点内**零副作用**，所以重跑绝对安全。

合成一个节点就会二选一：要么工单建两次，要么那句"已转接"消失。

工单创建另有幂等保护（`get_pending_escalation`），因为 resume 会重跑 `escalation_notice`。

`escalation_wait` resume 之后去的是 `feedback`，**刻意绕开 `responder`**。人工说完就算说完了，
模型此刻没有任何信息可生成，唯一的产出是把人工那句话复读一遍——实测人工说「稍等，我查询下」，
AI 收尾原样抄了一遍。收尾文案改由 `/close` 接口按模板拼，确定性产出。
详见 [docs/specs/002-human-handoff-lifecycle.md](docs/specs/002-human-handoff-lifecycle.md)。

### 3. RAG 是「向量 + BM25 → RRF → Reranker」的组合拳

- **纯向量**：能匹配"退款要多久" ↔ "退款到账时间"，但对订单号、`DDP` 这类专有名词不敏感
- **纯 BM25**：字面准，但用户换个说法就废（"钱什么时候退回来" 匹配不到"退款时效"）
- **RRF 融合**：只看排名不看分数，天然免去两路分数量纲不一致的问题
- **Reranker 精排**：融合后仍有 40 条候选，交叉编码器压到 5 条

多轮里用户会说"那它到哪了"，这种省略句直接检索必然跑偏，所以检索前加了一步 **query 改写**，把它补全成独立可检索的问句。

### 4. 知识库版本治理：失效政策必须检索不到

知识文档带 frontmatter（`status` / `version` / `effective_from` / `effective_until`），`status: deprecated` 的文档在**入库时**就被跳过，永远进不了索引。仓库里 `_deprecated-shipping-2025.md` 就是这条规则的回归样本——它有真实的旧运费标准，测试断言它绝不会出现在检索结果里。

分块 id 形如 `logistics.md::7`，文件中间插一段会让后续 id 全部错位、增量 upsert 留下孤儿向量。所以入库**每次全量重建**保证正确性，但按内容哈希缓存 embedding——文本没变的 chunk 不重复调接口。索引重建是毫秒级的，embedding 调用才是成本所在。

### 5. 澄清轮不算「未解决」

用户没给订单号时 Agent 必须反问。如果反问也被计入"未解决轮数"，Agent 问两次订单号就把用户转给人工了。所以 `needs_clarification` 的轮次**不累加** `unresolved_turns`。

### 6. 两个会话助手的提交语义必须一致

`session_scope()`（脚本/节点用）和 `get_db()`（FastAPI 依赖用）当初一个提交、一个不提交，这是个代价很高的不对称：写接口只调 `flush()` 的话，语句发出去了但事务没提交，请求结束时 `session.close()` 连同回滚一起丢掉。接口照样返回 `success: true` 和一个新生成的 id，只有真去查库才知道什么都没有。

症状还很会伪装——`POST /api/escalations/{id}/reply` 里后半段用的是 `session_scope()`，于是「收尾文本 + 会话置为 active」落库了，「人工回复 + 工单置为已解决」被回滚了，数据自相矛盾：会话已经 active，工单却永远停在 pending。

现在两个助手都正常返回即提交，写接口不再需要记得手写 commit。接口级测试一律**另开一个数据库会话去读**，只认真提交过的数据，所以这类问题不会再溜过去。

顺带一提，`/reply` 里在图 resume 之前必须显式提交一次：节点内部会另开 `session_scope()` 写库，而 SQLite 同一时刻只允许一个写事务，本会话持有的写锁会和它撞成 `database is locked`。

### 7. 召回用宽查询，排序要用原问句

意图扩展词（`物流 包裹 时效 轨迹 清关 派送`）拼进 query 是为了**扩大召回**——用户说「那它到哪了」，不补词就召不回东西。但同一串 query 如果也拿去排序，就变成在给「恰好含这些泛化词的 chunk」投票：

「包裹寄到德国一般几天」实测会把「轨迹长时间不更新」「派送失败」顶到前面，真正写着 `8–14 个工作日` 的那条掉到**第 6 名**——而 `rerank_top_n=5`，正好被切掉。检索没报错、照样返回 5 条，只是没有一条写着答案。

雪上加霜的是 RRF 的 `1/(k+rank)` 在 k=60 时刻意压得很平：同一批候选里第 1 名与第 6 名只差 6%，截断点近乎随机。所以 `RERANK_PROVIDER=none` 原来的实现（`docs[:top_n]`）等于把「谁进前 5」交给了一个随机数。

现在 `none` 的含义从「不精排」纠正为「不用模型精排」：拿**未扩展的原始问句**对候选做词面重排，复用语料 BM25 的 IDF，所以「德国」这类稀有词的权重天然高于「包裹」。实测要点覆盖率 92.4% → 98.0%，完全通过 92.0% → 98.0%。精排服务不可用时也退到这条路，而不是退回纯截断。

### 8. mock 模式的边界写清楚

`LLM_PROVIDER=mock` 是**关键词 + 模板拼接的桩，不是语言模型**。它的存在只是让「没有 key 也能端到端跑通全图、跑通前端、跑通单测、CI 零外部调用」。所有 mock 输出都带标记，`/api/health` 和前端会显式提示当前处于离线演示模式，避免把桩输出误当成真实模型能力。

---

## 技术栈

| 层 | 选型 |
|---|---|
| Agent 编排 | LangGraph（`StateGraph` + `interrupt`/`Command` + SQLite checkpointer） |
| 后端 | Python 3.12 / FastAPI / SQLAlchemy 2.0 async / SSE（`sse-starlette`） |
| LLM | OpenAI 兼容接口，可切 DeepSeek / Qwen / Ollama；分类与生成分温度 |
| 检索 | 向量（Qdrant 或进程内本地库）+ BM25（`rank-bm25` + jieba）+ RRF + Reranker |
| 前端 | React + Vite + TypeScript，SSE 流式打字机 |
| 存储 | 开发 SQLite，生产可换 PostgreSQL（改 `DATABASE_URL` 即可，模型层不动） |

---

## 目录结构

```
cs-agent/
├── backend/
│   ├── app/
│   │   ├── api/            chat(SSE) / sessions / escalation / feedback / health / console(登录)
│   │   ├── security.py     后台鉴权：口令比对、令牌签发与校验、角色依赖、登录限流
│   │   ├── graph/          LangGraph 装配
│   │   │   ├── builder.py      图的边与条件边
│   │   │   ├── state.py        AgentState（TypedDict）
│   │   │   ├── prompts.py      所有 prompt 集中于此，便于对比调优
│   │   │   ├── emitter.py      SSE 事件发射
│   │   │   └── nodes/          6 个节点
│   │   ├── rag/            chunker / embedder / bm25 / hybrid / rerank / store / ingest
│   │   ├── tools/          order_tools + registry（function calling schema）
│   │   ├── db/             models / repository / seed
│   │   ├── llm.py          对外只暴露 json / text / stream / with_tools 四个方法
│   │   └── llm_mock.py     离线桩
│   ├── data/knowledge/     知识库 markdown（9 篇，含 1 篇失效样本）
│   ├── evals/              100 条检索评测集（jsonl）
│   ├── scripts/            ingest.py 入库 CLI / eval.py 离线评测
│   └── tests/              图流程 / 检索层 / 接口层
├── frontend/src/
│   ├── pages/              ChatPage(公开) / LoginPage / DeskPage(客服) / AdminPage(管理员)
│   ├── auth/               session（令牌存取与失效广播）/ AuthContext
│   ├── components/         消息气泡 / 思考链 / 工具卡片 / 引用面板 / 转人工横幅 / 评价卡 / 后台页头 / 路由守卫
│   ├── hooks/              chatReducer（SSE 状态机）/ useChat / useSessions
│   └── api/                client / sse / stream / types
└── docs/API.md             前后端接口契约
```

### 已注册工具

| 工具 | 作用 |
|---|---|
| `query_order` | 查订单状态、金额、商品、下单时间 |
| `query_logistics` | 查物流轨迹与预计送达 |
| `calc_refund_fee` | 按退货原因和目的地算退运费与到账时间 |
| `create_ticket` | 建工单转专项团队 |

---

## 配置

全部配置项见 **[backend/.env.example](backend/.env.example)**（含逐项注释）。最常调的几项：

| 变量 | 说明 |
|---|---|
| `LLM_PROVIDER` | `openai` / `deepseek` / `qwen` / `ollama` / `mock` |
| `EMBEDDING_PROVIDER` | `openai` / `dashscope` / `qwen` / `local` / `hashing` |
| `QDRANT_URL` | 留空 → 自动降级为进程内本地向量库 |
| `RETRIEVE_TOP_K` / `RERANK_TOP_N` | 召回 20 → 精排 5 |
| `CHUNK_SIZE` / `CHUNK_OVERLAP` | 420 / 80 |
| `ESCALATION_MAX_UNRESOLVED_TURNS` | 同一问题连续 N 轮未解决 → 转人工（默认 2） |
| `ESCALATION_MAX_DISSATISFACTION` | 用户连续 N 次不满 → 转人工（默认 2） |
| `CONSOLE_AGENT_PASSWORD` / `CONSOLE_ADMIN_PASSWORD` | 客服 / 管理员口令。两个都留空 = 后台整体关闭 |
| `CONSOLE_TOKEN_SECRET` | 令牌签名密钥（≥16 位）。留空同样视为后台关闭 |
| `CONSOLE_TOKEN_TTL_MINUTES` | 令牌有效期，默认 720（12 小时，够一个班次） |

### 后台账号与角色

后台**不是账号体系**，是两套共享口令（客服一套、管理一套），换 HMAC 签名的令牌。
配置留空时后台整体关闭、受保护接口一律 503——**失败方向是关闭而不是放行**。

| 角色 | 能进 | 口令变量 |
|---|---|---|
| 客服 `agent` | `/desk` 工单台（转人工的工单队列、上下文、回复、结单） | `CONSOLE_AGENT_PASSWORD` |
| 管理员 `admin` | `/desk` + `/admin` 满意度看板（经营数据） | `CONSOLE_ADMIN_PASSWORD` |

用户端（`/chat`）始终公开，且**页面上不放任何后台入口**。

**已知限制**（刻意留着，不是遗漏）：口令共享 → 无法追责到具体某个人，客服署名只是缓解手段；
令牌存 localStorage → XSS 能读走；`DELETE /api/sessions/{id}` 没有会话归属校验，
知道 `session_id` 就能删（本项目没有用户体系，会话号即凭证）。三条都记在
[docs/specs/001-console-auth-and-roles.md](docs/specs/001-console-auth-and-roles.md) §10。

#### 生产口令从哪来（这里刻意不写口令本身）

`.env.example` 里这三项留空、模板里不留任何默认口令，生产口令由
[deploy/cicd-deploy.sh](deploy/cicd-deploy.sh) **首次部署时随机生成**，不经过任何人的手：

```
openssl rand -base64 12  →  14 位客服/管理口令
openssl rand -hex 32     →  签名密钥
```

生成后只落两个地方，都是 `chmod 600`（仅属主可读）：

| 位置 | 用途 |
|---|---|
| 服务器 `backend/.env` | 服务启动时读取 |
| 服务器 `~/.veyacare-console-credentials.txt` | 给人看，登录时来这里取 |

**口令不进仓库，也不进部署日志。** 部署脚本明明能直接 `echo` 出来却选择写文件，
README 这一节同样只写「去哪取」不写「是什么」——原因一样，而且对 README 更硬：
**这个仓库是公开的**，README 里的一行字会随提交进入 git 历史，删掉之后历史里还在，
fork 和爬虫也早就抄走了。后台队列里是用户的对话原文，看板是经营数据，
这两样东西不该由一份公开文档来守门。

取口令：

```bash
ssh <服务器> 'cat ~/.veyacare-console-credentials.txt'
```

轮换：改服务器 `backend/.env` 里的 `CONSOLE_*` 三项后重启服务即可，没有别的状态要同步
（令牌是签名制的，换密钥等于让所有已发出的令牌立刻失效——这也是唯一的「踢人下线」手段）。

> 本机开发想开后台，随便给三个值就行，不用去线上偷：
> `CONSOLE_AGENT_PASSWORD=dev-agent CONSOLE_ADMIN_PASSWORD=dev-admin CONSOLE_TOKEN_SECRET=$(openssl rand -hex 32)`

---

## 测试

```bash
.venv/Scripts/python -m pytest        # 127 passed（在仓库根或 backend/ 下跑都一样）
```

> `pytest.ini` 特意放在**仓库根**而不是 `backend/` 下。配置若只存在于 `backend/`，从仓库根直接敲 `pytest` 就找不到它，`asyncio_mode` 与 loop scope 两项设置随之失效 —— session 级异步 fixture 与测试落到不同事件循环上，图与 checkpointer 的 aiosqlite 连接跨 loop 复用，转人工 resume 失效。表现是当时 52 条里 23 条报错，而代码一行没坏，排查时只能看到 `GraphInterrupt` 和「收尾回复为空」这类业务断言。

分五层：`test_graph.py` 直接驱动图，`test_llm_mock.py` 打桩自己的规则表，`test_rerank.py` 打检索层，`test_config.py` 打配置解析，其余三个文件走 HTTP 打真实接口。

**图流程（`test_graph.py`，12 条）** —— 改任何节点、任何 prompt、任何检索参数，只要主链路跑挂了这里就会红：

- 完整链路（意图 → RAG → 工具 → 回复），并断言回复里引用的是**真实轨迹**而非编造
- 闲聊短路（不检索、不调工具）
- 缺订单号时**反问**而不是编一个物流状态
- 查不到的订单**如实说查不到**
- 退货运费来自工具计算，不是模型臆测
- **失效文档永远检索不到**
- 转人工：挂起 / 工单幂等（interrupt 重跑不建两张单）/ 回复与结单分离 / 结单后 resume 且 AI 不复读
- 多轮上下文保留；评分邀请不跨轮残留
- 纯政策问题不借用上一轮的订单号 —— 拿真数据答错题比查不到更糟

**桩的规则表（`test_llm_mock.py`，9 条）** —— 离线模式下 CI 跑的就是桩，桩的规则表跑偏等于整张回归网跟着偏，所以它本身也要被测试盯住：

- 槽位只在「本轮明确回指上一轮」或「本轮毫无线索」时才沿用上文；纯政策问题不许继承上一轮的订单号
- 同一标识符只归属一个槽位：订单号 `SO20260928001` 恰好也满足运单号模式，曾被两个正则同时认领
- 反向也钉住：真·多轮指代（"那它到哪了"）必须**仍然**继承 —— 修跨轮继承时最容易顺手把正常追问一起打死

**检索层（`test_rerank.py`，8 条）** —— 钉住下面那条设计决策 7：

- 答案片段必须活过 `top_n` 截断（修之前它排第 6，必然红）
- 只含泛化扩展词的片段不许排在真正回答问题的片段前面
- 字面命中查询词的候选会被提上来；零重叠时老实退回 RRF 序；排序可复现

**配置层（`test_config.py`，4 条）** —— 直接拿仓库里那份 `.env.example` 去解析，钉住「照 README 走 `cp .env.example .env` 必须能起来」。这条测试是有来由的：`.env.example` 给可选配置留了空值（`EMBEDDING_DIM=` 留空表示自动探测），而 pydantic 会拿空字符串去解析 `int` 直接抛 `ValidationError`，于是文档推荐的第一步反而把服务弄挂。本地一直没暴露，因为本地从没真的建过 `.env`。

**接口层（29 条）** —— 断言一律「开一个全新的数据库会话去读」，只认真提交过的数据，因此能抓到「接口报成功、数据其实没落库」这类光看返回值发现不了的问题：

- `test_feedback.py` —— 评价提交后看板真的看得到（含低分告警分支）、均分跟着变、会话意图带进看板、越界评分与超长备注在校验层被拒、自动解决率与计数自洽
- `test_escalation_api.py` —— 走真实对话接口触发转人工 → 人工回复落库但**工单仍待处理**、可以连回几次、结单后才置为已解决且会话回到 active；挂起期间 AI 不许抢答、用户补的话不丢且计入未读；收尾文案不是人工回复的复读；处理完能正常恢复对话；结单后不能再回复
- `test_sessions_api.py` —— 会话列表与消息读取、`awaiting_human` 轮询契约随**结单**（而非回复）开关、删除真的删掉且不留下孤儿消息/工单、不误删其它会话

前端另有一份契约校验，把 `docs/API.md §1.1` 的 SSE 示例原样喂给 reducer：

```bash
cd frontend
npm run check:reducer                 # 36 条断言
```

它校验的是 `chatReducer` 这个纯函数的状态机行为（转人工、静默轮询不冲流式回合、错误重试、满意度状态机等 8 个场景），**不渲染组件**，所以覆盖不到「组件读了接口的 `null` 字段而崩」这类问题。

那类问题由接口契约校验兜 —— 起真实服务，用真实 HTTP 把前端会读的每个字段打一遍：

```bash
cd backend
.venv/Scripts/python scripts/verify_contract.py http://127.0.0.1:8000   # 默认打线上
```

它比对的是「接口实际返回的类型」与「前端声明的类型」。这一步非有不可，因为 [frontend/src/api/types.ts](frontend/src/api/types.ts) 里的类型是**手写断言、不是从接口推导的**：满意度看板的 `avg_rating` 在无人评分时返回 `null`，而类型写的是 `number`，`tsc` 一路绿灯，直到用户点开看板才 `TypeError: Cannot read properties of null`。允许为空的字段必须登记在脚本的 `NULLABLE_FIELDS` 白名单里并注明前端在哪处理的，否则同样报错。两个 job 都会在 CI 里跑。

---

## 离线评测

```bash
cd backend
.venv/Scripts/python -m scripts.eval                 # 跑全量，打印报告
.venv/Scripts/python -m scripts.eval --verbose       # 每条明细
.venv/Scripts/python -m scripts.eval --min-hit-rate 0.9   # 门禁：低于阈值非 0 退出
```

评测集 `backend/evals/retrieval_set.jsonl`，**100 条**，覆盖 8 篇有效文档（物流 17 / 退货 16 / 关税 15 / 支付 13 / 改单 12 / 尺码 11 / 优惠券 9 / 转人工 7）。问题一律写成真实买家的口语（「日本韩国的话多久能收到啊」「我卡在两个码中间该怎么挑」），不是把文档标题改成问句。

| 指标 | 当前 |
|---|---|
| 来源命中率 Hit@5 | 100.0% |
| 首个正确来源 MRR | 0.9567 |
| 答案要点覆盖率 | 98.0% |
| 完全通过（来源 + 要点） | 98.0% |
| 意图分类准确率 | 90.0% |
| 失效文档污染 | 0 次 |

**为什么不测「回答是否正确」**：离线跑的是 mock 模型，答案是关键词模板拼的，测它只是在测模板本身。所以只测检索这一段——不依赖任何外部 API、结果确定、可回归，而且检索错了后面全错。

**两个口径刻意分开算**：检索指标用**标注意图**，把分类误差隔离在检索之外；意图指标单独拿分类器输出对比标注。合在一起算的话，分类错了会污染检索得分，看不出该优化哪一段。

还有一条负向断言：评测集里埋了针对失效文档的对抗样本（如「你们满多少包邮」期望 `$29` 而不是旧政策的 `$49`），`pollution_count` 非 0 直接让脚本以非 0 退出。

---

## 部署

线上跑在**香港服务器**上，原生部署：

```
push main → GitHub Actions → SSH → /opt/veyacare/deploy/cicd-deploy.sh
```

| 组件 | 做法 | 为什么这么选 |
|---|---|---|
| 后端 | venv + PM2 跑 `uvicorn`，只听 `127.0.0.1:3002` | 服务器上没有 Docker，且可用内存只剩 1.1G |
| 前端 | `npm run build` 出静态文件，nginx 直接托管 | 静态文件几乎不占常驻内存 |
| 网关 | nginx：`/` 吐静态文件、`/api/**` 反代到 `3002` | 前端走相对路径 `/api`，同源部署天然没有跨域 |

**部署脚本是幂等的**，可安全重复执行：nginx 配置内容没变就不覆盖、不白 reload。它会自动安装 nginx 站点配置、重建检索索引、构建前端（先出到 `dist.new` 再原子替换，避免构建途中用户拿到写了一半的资源）、`pm2 startOrReload`，最后做端到端自检（后端 `/api/health` + 经 nginx 的首页与接口），任一步失败立刻非 0 退出并打印日志。

**`docker-compose.yml` 仍然保留**，用于本地一条命令起完整环境；服务器上没用它，是因为 **Docker 会改 iptables 且要常驻约 200M 内存，而这台机器上还跑着另外两个线上站点**。

首次部署需要在仓库配两个 Secret：`VEYACARE_HOST`（服务器 IP）与 `VEYACARE_SSH_KEY`（部署私钥全文）。

**HTTPS 已于 2026-09-28 签好上线**（`https://cs.veyawork.work`，Let's Encrypt，certbot 的 systemd timer 自动续期）。

这里有个容易踩的坑，值得单独记一笔：`deploy/nginx-cs.veyawork.work.conf` 是**服务器现状的准源头**，不是只含 80 端口的手写草稿。部署脚本第 7 步拿它与服务器上那份 `diff`，不一致就覆盖 + reload —— 当初跑 `certbot --nginx` 时 certbot 就地改写了服务器上的配置（拆出 80→443 的 301 段、给主 server 块补上 443 ssl 与证书路径），**如果仓库这份没跟着同步回来，下一次 push 就会把整个 HTTPS 段冲掉，站点静默退回纯 HTTP**。所以签完证书后把它逐字节拷了回来（md5 一致），只在注释头补了说明。以后若再动 certbot，记得同样同步一次。

## 当前进度

**已完成**

- 完整链路跑通：意图识别 → RAG → 工具调用 → 流式回复 → 满意度评估
- 4 个真实工具（超出"至少 3 个"的要求）
- 转人工挂起 / 恢复闭环，工单落库且上下文完整
- **转人工有了人这一端**：客服在 `/desk` 看到工单队列与 AI 当时的完整上下文（原话、意图、槽位、工具调用、检索资料），可以分几次回复，办完点「结束会话」才交还 AI；工单带未读数，用户在等待期间补的话不会丢；管理看板在 `/admin`，两者都要口令登录，用户端不含任何后台入口
- 离线 mock 模式，无 key 可跑通全图与全部单测
- 前端 SSE 流式打字机 + 思考链 / 工具卡片 / 引用面板 / 转人工横幅 / 满意度评价
- 满意度闭环（`POST /api/feedback` → 看板可见）与转人工、会话管理三条接口链路均有接口级测试
- 100 条评测集 + 可回归的评测脚本，检索 Hit@5 100% / 要点覆盖 98%
- Dockerfile（后端 + 前端多阶段）、docker-compose、GitHub Actions（测试 + 评测门禁 + 镜像冒烟）
- 后端 127/127 测试通过（含 36 条鉴权用例）；前端 reducer 契约 36 条断言通过；接口契约校验全项通过（含 7 条受保护接口的未授权/越权检查，项数随库中会话数浮动）
- **已部署上线**：香港服务器原生部署（PM2 + nginx），push 到 main 自动发布，见「部署」一节

**未完成**

- 生产环境仍是 SQLite，`DATABASE_URL` 换 PostgreSQL 的路径通但没跑过
- 前端只做到响应式布局，未专门针对小程序 / 原生端做适配
- 线上跑的是 mock 模式（离线演示）。接真模型只需改服务器上 `backend/.env` 里的 provider 与 key
