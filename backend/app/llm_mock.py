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

_INTENT_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("human_agent", ("转人工", "人工客服", "真人", "找个人", "投诉", "你们领导", "客服电话")),
    ("logistics", (
        "物流", "快递", "包裹", "到哪", "发货", "清关", "签收", "运单", "tracking", "没收到", "还没到",
        # 运费/包邮门槛也是「物流政策」，属于要查知识库的问题，不能落到 chitchat 去
        "运费", "邮费", "快递费", "包邮", "物流费", "配送费",
    )),
    ("return_refund", ("退货", "退款", "换货", "退了", "退我", "返回", "refund", "return", "七天", "无理由")),
    ("customs_duty", ("关税", "被税", "税费", "清关材料", "ddp", "ddu", "海关", "duty", "tax")),
    ("size_fit", ("尺码", "码数", "穿几码", "size", "偏大", "偏小", "身高", "体重", "胸围")),
    ("payment", ("支付", "付款", "扣款", "付不了", "刷卡", "信用卡", "paypal", "payment", "扣了钱")),
    ("coupon", ("优惠券", "券", "折扣码", "优惠码", "coupon", "满减", "活动价")),
    ("order_change", ("改地址", "修改订单", "取消订单", "改尺码", "改颜色", "合并订单", "还没发货想改")),
]

_ORDER_NO_RE = re.compile(r"\b(SO\d{10,16}|\d{12,20})\b", re.IGNORECASE)
_TRACKING_RE = re.compile(r"\b([A-Z]{2}\d{9,13}|LP\d{10,14})\b", re.IGNORECASE)
_SKU_RE = re.compile(r"\b([a-z]{2}\d{6,10})\b", re.IGNORECASE)
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
_AMOUNT_RE = re.compile(r"(?:\$|USD\s?)(\d+(?:\.\d{1,2})?)", re.IGNORECASE)

_COUNTRIES = ("美国", "英国", "德国", "法国", "西班牙", "意大利", "日本", "韩国", "巴西", "沙特", "阿联酋", "澳大利亚", "加拿大")

_DISSATISFIED_WORDS = ("太差了", "什么破", "垃圾", "投诉", "差评", "失望", "气死", "怎么回事", "搞什么", "骗人", "退钱", "不想等")


def classify_intent(question: str, history: list[str] | None = None) -> dict:
    """关键词意图分类。取命中词最多的一类，全不命中则 chitchat。"""
    # 只有在最新一句话里找不到线索时，才回看上一轮用户消息（指代消解，如"那它到哪了"）
    haystacks = [question] + list(history or [])[:1]

    best_intent, best_score = "chitchat", 0
    for text in haystacks:
        low = text.lower()
        for intent, words in _INTENT_KEYWORDS:
            score = sum(1 for w in words if w in low)
            if score > best_score:
                best_intent, best_score = intent, score
        if best_score:
            break

    return {"intent": best_intent, "slots": extract_slots(" ".join(haystacks))}


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
