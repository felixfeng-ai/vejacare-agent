"""离线演示模式（LLM_PROVIDER=mock）的桩实现。

存在的意义：
1. 没有 API key 时也能端到端跑通整张图、跑通前端、跑通单测
2. CI 里不产生任何外部调用与费用

**它不是语言模型**，输出来自关键词规则与模板拼接。所有生成结果都带 `mock: true` 标记，
`/api/health` 与前端都会显式提示当前处于离线演示模式，避免把桩输出误当成真实模型能力。
"""

from __future__ import annotations

import json
import re

# ---------------------------------------------------------------- 意图关键词

#: 意图关键词表。**表序即优先级**，只用于打分打平的时候。
#: 顺序刻意从「具体」排到「宽泛」：`logistics` 的「发货」「包裹」在很多别的问题里也会出现，
#: 让它垫底，打平时才不会把「订单已经发货了还能改地址吗」判成物流咨询。
_INTENT_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("human_agent", (
        "转人工", "人工客服", "真人", "找个人", "投诉", "你们领导", "客服电话", "转接", "找客服",
        "人工", "活人", "客服在吗", "要验证码", "工单",
    )),
    # 改单/取消：几乎每条都同时含「订单」「发货」，必须靠更长的关键词压过 logistics
    ("order_change", (
        "改收货地址", "改地址", "修改地址", "地址改", "地址最多", "改到另一个国家", "收件人电话",
        "取消订单", "订单还能取消", "修改订单", "合并订单", "合并成", "合单", "拆单", "拒收",
        "改尺码", "改颜色", "换一个款", "改款", "换码", "电话",
    )),
    ("return_refund", (
        "退货", "退款", "换货", "退了", "退我", "返回", "refund", "return", "七天", "无理由",
        "能退", "退吗", "退不", "退回去", "退货窗口", "退货运费", "邮费算谁", "运费算谁",
        "运费谁出", "运费谁承担", "到账", "仓库验收", "商品状态", "不喜欢", "不合身", "换尺码",
        "换个尺码", "换个码", "退到",
    )),
    ("customs_duty", (
        "关税", "被税", "税费", "ddp", "ddu", "海关", "duty", "tax", "包税", "免税", "起征点",
        "申报", "避税", "查验", "被扣", "海关扣", "放行", "交税", "多少税", "要交多少", "材料",
    )),
    ("payment", (
        "支付", "付款", "扣款", "付不了", "刷卡", "信用卡", "paypal", "payment", "扣了钱", "扣两次",
        "3d 验证", "3d验证", "验证超时", "预授权", "风控", "账单地址", "支付失败", "付款方式",
        "银联", "货到付款", "不付款", "解冻", "被扣了两次", "钱扣了",
    )),
    ("coupon", (
        "优惠券", "折扣码", "折扣券", "优惠码", "coupon", "满减", "活动价", "券", "免邮券",
        "价保", "退差价", "降价", "用券",
    )),
    ("size_fit", (
        "尺码", "码数", "穿几码", "穿什么码", "size", "偏大", "偏小", "身高", "体重", "胸围",
        "腰围", "臀围", "版型", "大一码", "小一码", "大半码", "两个码", "欧码", "美码", "脚宽",
        "怎么量", "怎么挑", "面料", "氨纶", "买大", "买小",
    )),
    # 垫底：最宽泛的一类
    ("logistics", (
        "物流", "快递", "包裹", "到哪", "发货", "清关", "签收", "运单", "tracking", "没收到",
        "还没到", "运费", "邮费", "快递费", "包邮", "物流费", "配送费", "时效", "派送", "派件",
        "轨迹", "查件", "承运商", "暂扣", "加急", "送到", "收货", "到货", "多久", "几天", "延迟",
        "到达海关",
    )),
]

_ORDER_NO_RE = re.compile(r"\b(SO\d{10,16}|\d{12,20})\b", re.IGNORECASE)
# 运单号不能只认「两个字母 + 一串数字」：订单号 SO20260928001 恰好也长这样，
# 于是同一串字符被两个正则同时认领，前端槽位标签上就变成
# 「订单号：SO20260928001 运单号：SO20260928001」——用户看到的是我们连
# 运单号都查出来了，实际那个位置填的是他自己的订单号。
# 排除 SO 前缀（订单号专属），一个标识符只归属一个槽位。
_TRACKING_RE = re.compile(r"\b(?!SO\d)([A-Z]{2}\d{9,13}|LP\d{10,14})\b", re.IGNORECASE)
_SKU_RE = re.compile(r"\b([a-z]{2}\d{6,10})\b", re.IGNORECASE)
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
_AMOUNT_RE = re.compile(r"(?:\$|USD\s?)(\d+(?:\.\d{1,2})?)", re.IGNORECASE)

_COUNTRIES = ("美国", "英国", "德国", "法国", "西班牙", "意大利", "日本", "韩国", "巴西", "沙特", "阿联酋", "澳大利亚", "加拿大")

_DISSATISFIED_WORDS = ("太差了", "什么破", "垃圾", "投诉", "差评", "失望", "气死", "怎么回事", "搞什么", "骗人", "退钱", "不想等")


def _intent_score(text: str) -> tuple[str, int]:
    """给单段文本打分，返回得分最高的意图与分数。"""
    low = text.lower()
    best, best_score = "chitchat", 0
    for intent, words in _INTENT_KEYWORDS:
        score = sum(len(w) for w in words if w in low)
        if score > best_score:
            best, best_score = intent, score
    return best, best_score


#: 回指标记：出现这些词，才认为本轮是在接着上一轮那一单说。
#:
#: 槽位要不要沿用上文，判据是「本轮有没有指向上一轮那个实体」，而不是「本轮是不是
#: 在问政策」——「那退货运费怎么算？」既踩中政策问法（含「怎么算」），又确实该沿用
#: 订单号，用政策判据会把这类正常追问一起打掉。
_REFERENTIAL_MARKERS = (
    "那", "它", "这单", "此单", "该单", "这一单", "这个订单", "这订单",
    "我的订单", "我这单", "我的包裹", "上一单", "刚才", "刚说", "上面说",
)


def _refers_to_previous(question: str) -> bool:
    return any(marker in question for marker in _REFERENTIAL_MARKERS)


def classify_intent(question: str, history: list[str] | None = None) -> dict:
    """关键词意图分类。得分最高者胜，全不命中则 chitchat。

    打分是**命中关键词的长度之和**，不是命中个数。早期版本数个数，
    于是「订单已经发货了还能改地址吗」里 `发货` 与 `改地址` 各命中一个算打平，
    胜负就交给了关键词表里的先后顺序——而那个顺序本来是随手写的。
    按长度加权等于按「这个说法有多具体」加权：`改地址`(3) 压过 `发货`(2)，
    因为会说到「改地址」的人不太可能在问时效。

    打平时仍然由表序决定，但表序现在是有依据的（具体意图在前，宽泛的 logistics 垫底）。
    """
    # 只有在最新一句话里找不到线索时，才回看上一轮用户消息（指代消解，如"那它到哪了"）。
    #
    # 取 `[-1:]` 而不是 `[:1]`：history 是按时间正序传进来的，"上一轮"是**最后**一条。
    # 原来取的是 `[:1]`，即窗口里**最旧**的那一条，于是"再详细点"这种追问回看的是
    # 三轮之前那句，而不是刚聊完的那一轮——主题接不上，追问被当成闲聊或转人工处理。
    haystacks = [question] + list(history or [])[-1:]

    best_intent, best_score = "chitchat", 0
    own_score = 0
    for index, text in enumerate(haystacks):
        intent, score = _intent_score(text)

        # 回看上文时**不继承转人工诉求**：上一轮要过人工，这一轮问的是业务问题，
        # 就按业务问题分类。
        #
        # 这条守的是线上实测过的一个坑：用户问完退换货政策，追了一句"再详细点"，
        # 因为上一轮里有"真人客服"，这一句被判成 human_agent，AI 当场闭嘴转人工。
        # 追问只该继承**主题**，不该继承**诉求**——诉求是那一轮的事，说完了就完了。
        if index > 0 and intent == "human_agent":
            continue

        if index == 0:
            own_score = score
        if score > best_score:
            best_intent, best_score = intent, score
        if best_score:
            break

    # 槽位只在两种情况下沿用上文：本轮明确回指上一轮（"那它到哪了"），
    # 或本轮自己一点线索都没有（"怎么办？"，除了上文无从判断）。
    #
    # 早期实现是无条件把上文和本轮拼成整段再抽槽位（因为 prompt 当时写的也是
    # 「从整段对话里抽取」），于是上一轮的订单号会悄悄"变成"本轮的槽位：
    # 用户只问了一句「退换货政策是什么」，工具却拿着上一单的订单号查了真实数据，
    # 回复里就出现了「从退款中扣除 USD 4.99 作为退回运费」。
    #
    # 拿真实数据回答一个没人问的问题，比查不到更糟——它读起来完全像是真的，
    # 用户没有任何线索能察觉那串数字来自另一单。判不出来时宁可让用户重申订单号
    # （responder 有现成的索要话术，blocked_on 会走那条路）。
    inherit = _refers_to_previous(question) or own_score == 0
    slot_source = " ".join(haystacks) if inherit else question

    return {"intent": best_intent, "slots": extract_slots(slot_source)}


def extract_slots(text: str) -> dict:
    """正则抽槽位。抽不到一律 None。"""
    order = _ORDER_NO_RE.search(text)
    tracking = _TRACKING_RE.search(text)
    sku = _SKU_RE.search(text)
    email = _EMAIL_RE.search(text)
    amount = _AMOUNT_RE.search(text)
    country = next((c for c in _COUNTRIES if c in text), None)

    return {
        "order_no": order.group(1).upper() if order else None,
        "tracking_no": tracking.group(1).upper() if tracking else None,
        "sku": sku.group(1).lower() if sku else None,
        "country": country,
        "amount": float(amount.group(1)) if amount else None,
        "email": email.group(0) if email else None,
    }


# ---------------------------------------------------------------- 工具决策


#: 通用政策类问法：问的是「你们的规定」，不是「我这一单怎么样」。
#: 这类问题没有订单号也能用知识库答，不该反过来向用户索要订单号。
_POLICY_PHRASES = (
    "标准", "政策", "规则", "门槛", "条件", "怎么算", "怎么收", "怎么计",
    "多少", "包邮", "几天", "多久", "是什么",
)


def _is_policy_question(question: str) -> bool:
    low = question.lower()
    return any(p in low for p in _POLICY_PHRASES)


def decide_tool(intent: str, slots: dict, question: str) -> dict:
    """按意图 + 槽位决定调哪个工具。

    返回值里的 `blocked_on` 是给下游回复用的：非空表示「本该查、但缺这个参数」。
    回复方据此向用户索要，而不是自己再猜一遍——两处各写一套关键词，早晚走偏。
    """
    order_no = slots.get("order_no")
    low = question.lower()

    if intent == "logistics":
        if order_no or slots.get("tracking_no"):
            return {
                "tool": "query_logistics",
                "args": {"order_no": order_no, "tracking_no": slots.get("tracking_no")},
                "blocked_on": "",
                "thought": "用户在问包裹位置，且提供了订单号/运单号",
            }
        if _is_policy_question(question):
            return {
                "tool": None,
                "args": {},
                "blocked_on": "",
                "thought": "通用时效/运费政策咨询，检索知识库即可回答",
            }
        return {
            "tool": None,
            "args": {},
            "blocked_on": "order_no",
            "thought": "缺少订单号，需要先向用户索要",
        }

    if intent == "return_refund":
        if order_no:
            return {
                "tool": "calc_refund_fee",
                "args": {"order_no": order_no, "reason": "change_of_mind"},
                "blocked_on": "",
                "thought": "用户咨询退货，需要告知退货运费与到账时间",
            }
        return {
            "tool": None,
            "args": {},
            "blocked_on": "" if _is_policy_question(question) else "order_no",
            "thought": "缺少订单号",
        }

    if intent == "order_change" and order_no:
        return {
            "tool": "query_order",
            "args": {"order_no": order_no},
            "blocked_on": "",
            "thought": "改单前需先确认订单当前状态",
        }

    if intent == "payment" and order_no:
        return {
            "tool": "query_order",
            "args": {"order_no": order_no},
            "blocked_on": "",
            "thought": "核实订单是否创建成功、是否重复扣款",
        }

    if intent == "customs_duty" and order_no:
        return {
            "tool": "query_order",
            "args": {"order_no": order_no},
            "blocked_on": "",
            "thought": "需要订单的目的国与申报金额来算税",
        }

    # 质量类投诉需要建单转专项团队
    if any(w in low for w in ("少发", "漏发", "破损", "质量", "坏了", "错的", "发错")):
        return {
            "tool": "create_ticket",
            "args": {
                "order_no": order_no,
                "category": "quality",
                "description": question[:200],
            },
            "blocked_on": "",
            "thought": "涉及商品质量/少发，需建工单转专项团队",
        }

    return {"tool": None, "args": {}, "blocked_on": "", "thought": "通用政策咨询，检索知识库即可回答"}


# ---------------------------------------------------------------- 回复生成


def _dig_section(prompt: str, header: str) -> str:
    """从 responder 的 user prompt 里抠出某个【小节】的内容。

    不能要求小节头后面必须换行：RESPONDER_USER 里【用户意图】【已抽取信息】
    的内容与标题同行，其余小节才另起一行。两种排布都要吃得下。
    """
    m = re.search(rf"【{re.escape(header)}】[^\S\n]*(.*?)(?=\n【|\Z)", prompt, re.S)
    return m.group(1).strip() if m else ""


#: responder 用这些字符串表示「本小节没有内容」。
#: 判空必须同时吃下全角与半角括号：prompt 的格式由 responder 定义，
#: 这边只是个解析方，不该因为对方把 (无) 换成 （无） 就把空小节当成真内容。
_EMPTY_MARKERS = frozenset({"", "无", "(无)", "（无）", "none", "null", "[]", "{}"})


def _has_content(section: str) -> bool:
    """小节是否有实质内容。空小节会让下游编出「已为您处理」这类假回复。"""
    return bool(section) and section.strip().lower() not in _EMPTY_MARKERS


_ASK_ORDER_NO_REPLY = "麻烦您提供一下订单号（SO 开头的那串），我这边立刻帮您查。"


def _parse_slots(section: str) -> dict:
    try:
        parsed = json.loads(section)
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _failed_tools(tools: str) -> list[str]:
    """取工具失败的原因。失败必须如实转达，不能拿知识库政策糊弄过去。"""
    return re.findall(r"^- 工具 \S+（查询失败）：(.+)$", tools, re.M)


def _tool_lines(tools: str) -> list[str]:
    """从成功工具结果的 JSON 里抽几个要展示的字段。"""
    lines: list[str] = []
    for key, fmt in (
        ("latest", "当前位置：{v}"),
        ("status_text", "订单状态：{v}"),
        ("eta", "预计送达：{v}"),
        ("fee_text", "费用说明：{v}"),
        ("ticket_id", "工单号：{v}"),
    ):
        m = re.search(rf'"{key}"\s*:\s*"([^"]+)"', tools)
        if m:
            lines.append(fmt.format(v=m.group(1)))
    return lines


def _blocked_on(intent_label: str, slots: dict, question: str) -> str:
    """把回复里看到的中文意图标签翻译回 intent key，再问 decide_tool 缺什么。

    prompt 里给模型的是中文标签（「物流跟踪」），而 decide_tool 认的是英文 key，
    这里做个反查。标签表是唯一真相源，不另抄一份。
    """
    from app.graph.state import INTENT_LABELS

    intent = next((k for k, v in INTENT_LABELS.items() if v == intent_label), "")
    if not intent:
        return ""
    return str(decide_tool(intent, slots, question).get("blocked_on") or "")


def compose_reply(prompt: str) -> str:
    """把检索到的资料 / 工具结果拼成一段中文回复。

    这是模板拼接，不是生成——只用于离线演示与单测断言。
    优先级刻意对齐 RESPONDER_SYSTEM 的硬约束：工具失败 > 工具数据 > 索要订单号 > 知识库政策。
    知识库排最后，是因为拿通用政策去回答「我的包裹到哪了」本身就是一种编造。
    """
    intent_lines = _dig_section(prompt, "用户意图").splitlines()
    intent_label = intent_lines[0].strip() if intent_lines else ""
    kb = _dig_section(prompt, "知识库参考资料")
    tools = _dig_section(prompt, "工具查询结果")
    human = _dig_section(prompt, "人工客服回复")
    slots = _parse_slots(_dig_section(prompt, "已抽取信息"))
    question = _dig_section(prompt, "用户最新一句话")

    if _has_content(human):
        return f"收到，人工同事已经为您处理。我这边补充一下：{human}\n\n如还有其他问题，随时找我。"

    # 硬约束 1：查不到就说查不到，不许用政策话术盖过去
    if _has_content(tools):
        failures = _failed_tools(tools)
        if failures:
            return f"抱歉，{failures[0]}\n\n您可以核对一下订单号，也可以让我帮您转人工同事查。"

    lines = _tool_lines(tools) if _has_content(tools) else []

    # 硬约束 3：缺订单号时直接问用户要，不要假装查到了。
    # 判据复用 decide_tool 的 blocked_on，而不是在这里另写一套意图/关键词判断——
    # 同一个问题「该不该索要」，tool 节点和回复方必须是同一个答案。
    if not lines and _blocked_on(intent_label, slots, question) == "order_no":
        return _ASK_ORDER_NO_REPLY

    # 硬约束 2：政策类问题只依据知识库
    if _has_content(kb):
        first = kb.split("\n---\n")[0].strip()
        first = re.sub(r"^\[[^\]]*\]\s*", "", first)
        sentences = re.split(r"(?<=[。！？])", first)
        summary = "".join(sentences[:2]).strip()
        if summary:
            lines.append(f"政策说明：{summary}")

    if not lines:
        return "您好，我已经记录您的问题，正在为您核实，请稍等。"

    head = f"关于您的{intent_label or '问题'}，为您查到以下信息："
    body = "\n".join(f"- {line}" for line in lines[:4])
    return f"{head}\n{body}\n\n如果还需要进一步处理，随时告诉我。"


def evaluate_turn(question: str, answer: str) -> dict:
    """启发式轮次评估。"""
    low_q = question.lower()
    wants_human = any(w in low_q for w in ("转人工", "人工客服", "真人", "投诉"))
    dissatisfied = any(w in low_q for w in _DISSATISFIED_WORDS)

    clarifying = any(w in answer for w in ("麻烦提供", "麻烦您提供", "请提供", "需要您提供", "方便告诉我"))
    resolved = (not clarifying) and any(
        w in answer for w in ("预计", "已经", "可以", "为您查到", "政策说明", "已为您")
    ) and not wants_human

    return {
        "resolved": bool(resolved),
        "needs_clarification": bool(clarifying),
        "dissatisfied": bool(dissatisfied),
        "wants_human": bool(wants_human),
    }


def escalation_reply() -> str:
    from app.graph.prompts import ESCALATION_REPLY_TEMPLATE

    return ESCALATION_REPLY_TEMPLATE
