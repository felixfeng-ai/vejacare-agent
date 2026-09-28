"""离线桩（app.llm_mock）的槽位规则回归。

桩不是语言模型，它的行为全靠两张规则表：意图关键词表与正则槽位表。规则表一旦跑偏，
受损的不只是「演示不好看」——**CI 跑的、单测断言的、本机验收的全都是桩**，
所以桩的规则本身需要被测试盯住。

这里盯的是两类曾经真实发生过的错：

1. 槽位跨轮继承。桩把上一轮用户消息和本轮拼成整段再抽槽位（prompt 当时写的也是
   「从整段对话里抽取」），于是用户只问一句「退换货政策是什么」，工具却拿着上一单
   的订单号查了真实数据，回复里出现「从退款中扣除 USD 4.99 作为退回运费」。
   拿真实数据回答一个没人问的问题，用户完全无从分辨那串数字来自另一单。

2. 订单号被同时认成运单号。SO20260928001 是「两个字母 + 11 位数字」，
   正好也满足运单号正则，前端槽位标签上就变成
   「订单号：SO20260928001 运单号：SO20260928001」，看着像两个都查到了。
"""

from __future__ import annotations

from app.llm_mock import classify_intent, decide_tool, extract_slots

#: 用户上一轮的发言：一个自带订单号的物流问题。
PREV = "我的包裹到哪了？订单号 SO20260928001"

#: 用户本轮的政策问题：一个字都没提订单或商品。
POLICY_Q = "当前平台的退换货政策是什么，什么商品允许退换货"


def _effective_slots(question: str, history: list[str]) -> dict:
    """按 intent 节点的方式过一遍槽位：丢掉 None，只留真有值的。"""
    raw = classify_intent(question, history)["slots"]
    return {k: v for k, v in raw.items() if v is not None}


# ---------------------------------------------------------------- 槽位跨轮


def test_policy_question_does_not_inherit_previous_order_no():
    """用户只问平台规则时，不能沿用上一轮的订单号。

    这是 2026-09-28 线上报上来的场景：政策问题被答成了「扣 USD 4.99 退货运费」，
    而那 4.99 来自上一轮那个美国订单 —— 数字是真的，只是回答的不是他问的问题。
    """
    result = classify_intent(POLICY_Q, [PREV])

    assert result["intent"] == "return_refund", "政策问题该判成退换货意图"
    assert result["slots"]["order_no"] is None, "本轮没提订单，不该继承上一轮的订单号"


def test_policy_question_does_not_trigger_order_tool():
    """槽位对了，工具决策才会对：没有订单号就不该调退货运费工具。"""
    decision = decide_tool("return_refund", _effective_slots(POLICY_Q, [PREV]), POLICY_Q)

    assert decision["tool"] is None
    # 也不能反过来向用户索要订单号 —— 人家问的是平台规则，不是自己那一单
    assert decision["blocked_on"] == ""


def test_policy_question_without_policy_wording_still_does_not_inherit():
    """「你们支持退换货吗」踩不中政策词表，但同样是通用问题，一样不能继承。

    这条是防止有人把判据写成「是不是政策问题」——那条路会因为词表漏词而留下缺口，
    而缺口的表现就是又一次拿上一单的数据作答。
    """
    assert _effective_slots("你们支持退换货吗", [PREV]).get("order_no") is None


def test_referential_follow_up_still_inherits_order_no():
    """真·多轮指代必须保住：这是「那它到哪了」能查到物流的唯一途径。

    修跨轮继承时最容易顺手把这条一起打死，所以单独立一个用例钉住。
    """
    slots = _effective_slots("那它什么时候能送到？", [PREV])

    assert slots["order_no"] == "SO20260928001"
    assert decide_tool("logistics", slots, "那它什么时候能送到？")["tool"] == "query_logistics"


def test_referential_follow_up_about_fee_still_inherits_order_no():
    """「那退货运费怎么算？」既是政策问法又该继承订单号。

    这条专门卡住「政策问题就不继承」那种粗暴判据：该问法含「怎么算」，
    按政策判据会被打成通用问题，多轮追问就断了。
    """
    question = "那退货运费怎么算？"
    slots = _effective_slots(question, [PREV])

    assert slots["order_no"] == "SO20260928001"
    decision = decide_tool("return_refund", slots, question)
    assert decision["tool"] == "calc_refund_fee"
    assert decision["args"]["order_no"] == "SO20260928001"


def test_elliptical_follow_up_looks_back_when_it_has_no_signal():
    """本轮自己毫无线索时（「怎么办？」）除了上文无从判断，仍应沿用。

    这是继承规则的第二个分支：本轮没有自己的意图线索。没有它，
    「怎么办？」这类省略式追问会突然开始向用户索要订单号。
    """
    assert _effective_slots("怎么办？", [PREV]).get("order_no") == "SO20260928001"


def test_own_order_no_in_current_turn_always_wins():
    """本轮自己给了订单号，就与上文无关，照常查。"""
    question = "订单 SO20260928001 我想退货，要花多少钱？"
    slots = _effective_slots(question, [])

    assert slots["order_no"] == "SO20260928001"
    assert decide_tool("return_refund", slots, question)["tool"] == "calc_refund_fee"


# ---------------------------------------------------------------- 槽位互斥


def test_order_no_is_not_also_extracted_as_tracking_no():
    """订单号不能被同时认成运单号。

    SO20260928001 满足 `[A-Z]{2}\\d{9,13}`，曾被运单号正则一并认领，
    前端槽位标签上出现两个一模一样的值。API 文档里的示例写的本来就是
    `"tracking_no": null`，是代码跑偏了。
    """
    slots = extract_slots(PREV)

    assert slots["order_no"] == "SO20260928001"
    assert slots["tracking_no"] is None, "运单号位置填的是用户自己的订单号"


def test_real_tracking_no_is_still_extracted():
    """排除 SO 前缀不能把真正的运单号一起排掉（种子数据里都是 LP 开头）。"""
    assert extract_slots("运单号 LP00123456789 到哪了")["tracking_no"] == "LP00123456789"
