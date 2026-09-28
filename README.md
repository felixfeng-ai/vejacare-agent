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
│   │   ├── api/            chat(SSE) / sessions / escalation / feedback / health
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
│   ├── components/         消息气泡 / 思考链 / 工具卡片 / 引用面板 / 转人工横幅 / 评价卡
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

---

## 测试

```bash
cd backend
.venv/Scripts/python -m pytest        # 47 passed
```

分三层：`test_graph.py` 直接驱动图，`test_rerank.py` 打检索层，其余三个文件走 HTTP 打真实接口。

**图流程（`test_graph.py`，10 条）** —— 改任何节点、任何 prompt、任何检索参数，只要主链路跑挂了这里就会红：

- 完整链路（意图 → RAG → 工具 → 回复），并断言回复里引用的是**真实轨迹**而非编造
- 闲聊短路（不检索、不调工具）
- 缺订单号时**反问**而不是编一个物流状态
- 查不到的订单**如实说查不到**
- 退货运费来自工具计算，不是模型臆测
- **失效文档永远检索不到**
- 转人工：挂起 / 工单幂等（interrupt 重跑不建两张单）/ 人工回复后 resume 收尾
- 多轮上下文保留

**检索层（`test_rerank.py`，8 条）** —— 钉住下面那条设计决策 7：

- 答案片段必须活过 `top_n` 截断（修之前它排第 6，必然红）
- 只含泛化扩展词的片段不许排在真正回答问题的片段前面
- 字面命中查询词的候选会被提上来；零重叠时老实退回 RRF 序；排序可复现

**接口层（29 条）** —— 断言一律「开一个全新的数据库会话去读」，只认真提交过的数据，因此能抓到「接口报成功、数据其实没落库」这类光看返回值发现不了的问题：

- `test_feedback.py` —— 评价提交后看板真的看得到（含低分告警分支）、均分跟着变、会话意图带进看板、越界评分与超长备注在校验层被拒、自动解决率与计数自洽
- `test_escalation_api.py` —— 走真实对话接口触发转人工 → 人工回复落库、工单置为已解决、会话回到 active；挂起期间 AI 不许抢答；处理完能正常恢复对话；工单不能重复回复
- `test_sessions_api.py` —— 会话列表与消息读取、`awaiting_human` 轮询契约随人工回复正确开关、删除真的删掉且不留下孤儿消息/工单、不误删其它会话

前端另有一份契约校验，把 `docs/API.md §1.1` 的 SSE 示例原样喂给 reducer：

```bash
cd frontend
npx esbuild tmp-reducer-check.ts --bundle --format=esm --platform=node --outfile=tmp-reducer-check.mjs
node tmp-reducer-check.mjs            # 36 条断言
```

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

## 当前进度

**已完成**

- 完整链路跑通：意图识别 → RAG → 工具调用 → 流式回复 → 满意度评估
- 4 个真实工具（超出"至少 3 个"的要求）
- 转人工挂起 / 恢复闭环，工单落库且上下文完整
- 离线 mock 模式，无 key 可跑通全图与全部单测
- 前端 SSE 流式打字机 + 思考链 / 工具卡片 / 引用面板 / 转人工横幅 / 满意度评价
- 满意度闭环（`POST /api/feedback` → 看板可见）与转人工、会话管理三条接口链路均有接口级测试
- 100 条评测集 + 可回归的评测脚本，检索 Hit@5 100% / 要点覆盖 98%
- Dockerfile（后端 + 前端多阶段）、docker-compose、GitHub Actions（测试 + 评测门禁 + 镜像冒烟）
- 后端 47/47 测试通过，前端契约校验 36 条断言通过

**未完成**

- **还没有实际部署上线**：镜像与 CI 配置已就位，但本机没有 Docker，**构建与启动均未实测过**；也没有公网演示链接
- 生产环境仍是 SQLite，`DATABASE_URL` 换 PostgreSQL 的路径通但没跑过
- 前端只做到响应式布局，未专门针对小程序 / 原生端做适配
