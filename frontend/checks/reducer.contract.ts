// 契约校验：把 docs/API.md §1.1 的 SSE 示例原样喂给 reducer，检查状态机行为。
//
// 跑法：npm run check:reducer   （等价于 esbuild 打包到 .check/ 再 node 执行）
//
// 为什么单独放一个目录而不是 src/ 下：它不参与前端打包，只被 CI 与本地调用；
// 放在 src/ 里会被 tsc 的 include 扫进去，也会让人误以为它是运行时代码。
//
// 注意它校验的是 reducer 这个纯函数，不渲染组件 —— 所以覆盖不到
// 「组件读了接口的 null 字段而崩」这类问题（看板 avg_rating 那次就是）。
// 那类问题由 backend/scripts/verify_contract.py 打真实接口来兜。
import { chatReducer, initialState, toChatMessage } from '../src/hooks/chatReducer.ts';
import type { ChatState, ChatAction } from '../src/hooks/chatReducer.ts';
import type { Message } from '../src/api/types.ts';

let failed = 0;
function ok(name: string, condition: boolean, extra?: unknown): void {
  if (!condition) failed += 1;
  console.log(`${condition ? 'PASS' : 'FAIL'}  ${name}${condition ? '' : `   -> ${JSON.stringify(extra)}`}`);
}

function apply(state: ChatState, actions: ChatAction[]): ChatState {
  return actions.reduce((acc, action) => chatReducer(acc, action), state);
}

const AT = '2026-09-28T07:31:22.114Z';
const userMessage = {
  id: 'u_1',
  role: 'user' as const,
  content: '我的包裹到哪了？订单号 SO20260928001',
  intent: null,
  createdAt: AT,
};

/* ---------- 场景 1：契约 §1.1 的完整物流回合 ---------- */
let state = chatReducer(initialState(), { type: 'turn/start', userMessage });
ok('turn/start 后进入流式态', state.live !== null && state.messages.length === 1);

const beforeMessages = state.messages;
state = apply(state, [
  { type: 'sse/session', sessionId: '9f1c-uuid' },
  { type: 'sse/node', node: 'intent_classifier', label: '正在理解您的问题' },
  {
    type: 'sse/intent',
    intent: 'logistics',
    intentLabel: '物流跟踪',
    slots: { order_no: 'SO20260928001', tracking_no: null, sku: null },
  },
  { type: 'sse/node', node: 'kb_retriever', label: '正在检索知识库' },
  {
    type: 'sse/kb',
    docs: [{ title: '物流时效与轨迹查询', source: 'logistics.md', score: 0.83, snippet: '……' }],
  },
  {
    type: 'sse/tool',
    item: { kind: 'tool', tool: { name: 'query_logistics', status: 'running', args: { order_no: 'SO20260928001' } } },
  },
  { type: 'sse/token', text: '您的包裹' },
  { type: 'sse/token', text: '目前在美国洛杉矶分拨中心' },
  {
    type: 'sse/tool',
    item: {
      kind: 'tool',
      tool: {
        name: 'query_logistics',
        status: 'done',
        result: { tracking_no: 'LP00123456789', carrier: '4PX', status: 'in_transit' },
        summary: '包裹已到达洛杉矶分拨中心，预计 10-02 送达',
      },
    },
  },
  { type: 'turn/finish', messageId: 'm_01H', at: AT, intent: 'logistics' },
]);

ok('session 事件落库 session_id', state.sessionId === '9f1c-uuid', state.sessionId);
ok('done 后回到空闲态', state.live === null);
ok('消息数 = 用户 + 助手', state.messages.length === 2, state.messages.length);
ok('未原地修改旧 messages 数组', beforeMessages.length === 1 && state.messages !== beforeMessages);

const assistant = state.messages[1];
ok('助手消息存在', assistant !== undefined);
ok('token 累积拼接正确', assistant?.content === '您的包裹目前在美国洛杉矶分拨中心', assistant?.content);
ok('done 的 message_id 用作消息 id', assistant?.id === 'm_01H', assistant?.id);
ok('意图与中文标签', assistant?.intent === 'logistics' && assistant?.intentLabel === '物流跟踪');
ok('槽位透传', assistant?.slots?.order_no === 'SO20260928001');
ok('kb 文档挂在消息上', (assistant?.kbDocs ?? []).length === 1);

const timeline = assistant?.timeline ?? [];
const nodeItems = timeline.filter((item) => item.kind === 'node');
const toolItems = timeline.filter((item) => item.kind === 'tool');
ok('思考链 2 个节点', nodeItems.length === 2, nodeItems.length);
ok('同名工具卡片合并为 1 张（running -> done）', toolItems.length === 1, toolItems.length);
ok(
  '工具卡片最终为 done 且带 summary',
  toolItems[0]?.kind === 'tool' && toolItems[0].tool.status === 'done' && toolItems[0].tool.summary === '包裹已到达洛杉矶分拨中心，预计 10-02 送达',
  toolItems[0],
);
ok('timeline 顺序保持后端下发顺序', timeline.map((item) => (item.kind === 'node' ? item.node : item.tool.name)).join('|') === 'intent_classifier|kb_retriever|query_logistics');

/* ---------- 场景 2：转人工 ---------- */
let escalated = chatReducer(initialState(), { type: 'turn/start', userMessage });
escalated = apply(escalated, [
  { type: 'sse/session', sessionId: 's2' },
  { type: 'sse/escalated', info: { reason: 'no_solution', reasonLabel: '两轮未解决', ticketId: 'esc_1' } },
  { type: 'turn/finish', messageId: 'm_esc', at: AT, intent: 'return_refund' },
]);
const escMsg = escalated.messages[1];
ok('转人工后仍生成一条说明气泡', escalated.messages.length === 2 && escMsg?.escalated === true);
ok('转人工信息落到 state.escalation', escalated.escalation?.ticketId === 'esc_1');
ok('会话状态置为 escalated', escalated.status === 'escalated');
ok('没有 token 也能用 done.intent 兜底标签', escMsg?.intentLabel === '退换货', escMsg?.intentLabel);

/* ---------- 场景 3：人工回复后解锁 ---------- */
const humanMessage: Message = {
  id: 'm_human',
  role: 'human_agent',
  content: '已为您加急，预计 24 小时内更新物流',
  intent: null,
  created_at: AT,
  meta: {},
};
// 真实调用链里，历史消息先经 toChatMessage 映射再进 reducer
const afterHuman = chatReducer(escalated, {
  type: 'history/loaded',
  sessionId: 's2',
  status: 'escalated',
  messages: [toChatMessage(humanMessage)],
  keepLocal: false,
});
ok('历史里有人工回复 → humanAgentReplied', afterHuman.humanAgentReplied === true);
ok('人工回复后清除转人工锁定', afterHuman.escalation === null);
ok('人工回复角色映射保留', afterHuman.messages[0]?.role === 'human_agent');
ok('历史消息 intent 为空时不报错', afterHuman.messages[0]?.intentLabel === null);

/* ---------- 场景 4：静默轮询不冲掉正在流式的回合 ---------- */
let polling = chatReducer(initialState(), { type: 'turn/start', userMessage });
polling = chatReducer(polling, { type: 'sse/token', text: '正在查询' });
const polled = chatReducer(polling, {
  type: 'history/loaded',
  sessionId: 's3',
  status: 'active',
  messages: [],
  keepLocal: true,
});
ok('流式期间的静默轮询保留本地消息', polled.messages.length === 1 && polled.live !== null);

/* ---------- 场景 5：错误态 + 重试 ---------- */
let errored = chatReducer(initialState(), { type: 'turn/start', userMessage });
errored = apply(errored, [
  { type: 'sse/token', text: '抱歉' },
  { type: 'sse/error', message: '模型服务超时', at: AT },
]);
ok('error 事件固化成带错误的气泡', errored.messages[1]?.error === '模型服务超时' && errored.live === null);
ok('错误气泡保留已产出的正文', errored.messages[1]?.content === '抱歉');

const retried = chatReducer(errored, { type: 'turn/retry', dropFrom: 1 });
ok('retry 丢弃失败气泡并重新进入流式', retried.messages.length === 1 && retried.live !== null);

/* ---------- 场景 6：历史映射与切换会话 ---------- */
const mapped = toChatMessage({
  id: 'm_1',
  role: 'assistant',
  content: '你好',
  intent: 'coupon',
  created_at: AT,
  meta: {},
});
ok('历史消息映射出中文意图标签', mapped.intentLabel === '优惠券', mapped.intentLabel);

const switched = chatReducer(escalated, { type: 'session/switch', sessionId: 's9' });
ok('切换会话清空上一条会话的转人工状态', switched.escalation === null && switched.messages.length === 0 && switched.sessionId === 's9');
const reset = chatReducer(escalated, { type: 'session/reset' });
ok('新建会话回到干净状态', reset.messages.length === 0 && reset.sessionId === null && reset.escalation === null);

/* ---------- 场景 7：满意度状态机 ---------- */
let fb = chatReducer(initialState(), { type: 'sse/feedback', messageId: 'm_01H' });
ok('feedback_request 弹出评价卡', fb.feedback?.phase === 'pending' && fb.feedback.messageId === 'm_01H');
fb = chatReducer(fb, { type: 'feedback/submitting' });
ok('提交中状态', fb.feedback?.phase === 'submitting');
fb = chatReducer(fb, { type: 'feedback/failed', message: '网络错误' });
ok('提交失败可重试', fb.feedback?.phase === 'pending' && fb.feedback.error === '网络错误');
fb = chatReducer(fb, { type: 'feedback/submitted' });
ok('提交成功', fb.feedback?.phase === 'done');
fb = chatReducer(fb, { type: 'feedback/dismiss' });
ok('收起评价卡', fb.feedback === null);

/* ---------- 场景 8：空回合不留空气泡 ---------- */
let empty = chatReducer(initialState(), { type: 'turn/start', userMessage });
empty = chatReducer(empty, { type: 'turn/cancel' });
ok('取消的空回合不产生气泡', empty.messages.length === 1 && empty.live === null);

console.log(failed === 0 ? '\nALL PASS' : `\n${failed} FAILED`);
process.exit(failed === 0 ? 0 : 1);
