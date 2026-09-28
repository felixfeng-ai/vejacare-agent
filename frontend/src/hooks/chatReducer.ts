/**
 * 会话状态机的纯函数部分（不含 React，便于单独验证）。
 *
 * 设计要点：
 * - 所有分支都做不可变更新（返回新对象/新数组，不原地 push/splice）。
 * - 「正在流式输出的这一轮」单独放在 state.live，不混进 state.messages，
 *   这样追加 token 时只有实时气泡重渲染，历史消息（React.memo）保持不动。
 * - id 与时间戳由外部传进来，reducer 保持纯函数。
 */

import { intentLabelOf } from '../api/types';
import type {
  ChatMessage,
  EscalationInfo,
  GraphNode,
  Intent,
  KbDoc,
  Message,
  SessionStatus,
  Slots,
  TimelineItem,
} from '../api/types';

/** 转人工横幅在没有拿到 reason 时的兜底文案 */
const FALLBACK_ESCALATION: EscalationInfo = {
  reason: 'unknown',
  reasonLabel: '已转接人工客服',
  ticketId: null,
};

/** 本轮流式输出中累积的内容 */
export interface LiveTurn {
  timeline: TimelineItem[];
  kbDocs: KbDoc[];
  text: string;
  intent: Intent | null;
  intentLabel: string | null;
  slots: Slots | null;
  escalated: EscalationInfo | null;
}

export type FeedbackPhase = 'pending' | 'submitting' | 'done';

export interface FeedbackState {
  messageId: string;
  phase: FeedbackPhase;
  error: string | null;
}

export interface ChatState {
  sessionId: string | null;
  status: SessionStatus;
  messages: ChatMessage[];
  /** 正在流式输出的一轮；null 表示空闲 */
  live: LiveTurn | null;
  loadingHistory: boolean;
  /** 顶层轻提示（历史加载失败、非流式错误等） */
  notice: string | null;
  escalation: EscalationInfo | null;
  /** 历史里出现过 role === "human_agent" 的消息 → 解除转人工锁定 */
  humanAgentReplied: boolean;
  feedback: FeedbackState | null;
}

export type ChatAction =
  | { type: 'history/loading' }
  | { type: 'history/loaded'; sessionId: string; status: SessionStatus; messages: ChatMessage[]; keepLocal: boolean }
  | { type: 'history/failed'; message: string }
  | { type: 'session/switch'; sessionId: string }
  | { type: 'session/reset' }
  | { type: 'turn/start'; userMessage: ChatMessage }
  | { type: 'turn/retry'; dropFrom: number }
  | { type: 'turn/cancel' }
  | { type: 'turn/finish'; messageId: string | null; at: string; intent: Intent | null }
  | { type: 'sse/session'; sessionId: string }
  | { type: 'sse/node'; node: GraphNode; label: string }
  | { type: 'sse/intent'; intent: Intent; intentLabel: string; slots: Slots }
  | { type: 'sse/kb'; docs: KbDoc[] }
  | { type: 'sse/tool'; item: Extract<TimelineItem, { kind: 'tool' }> }
  | { type: 'sse/token'; text: string }
  | { type: 'sse/escalated'; info: EscalationInfo }
  | { type: 'sse/feedback'; messageId: string }
  | { type: 'sse/error'; message: string; at: string }
  | { type: 'notice/dismiss' }
  | { type: 'feedback/submitting' }
  | { type: 'feedback/submitted' }
  | { type: 'feedback/failed'; message: string }
  | { type: 'feedback/dismiss' };

export function emptyLiveTurn(): LiveTurn {
  return {
    timeline: [],
    kbDocs: [],
    text: '',
    intent: null,
    intentLabel: null,
    slots: null,
    escalated: null,
  };
}

export function initialState(): ChatState {
  return {
    sessionId: null,
    status: 'active',
    messages: [],
    live: null,
    loadingHistory: false,
    notice: null,
    escalation: null,
    humanAgentReplied: false,
    feedback: null,
  };
}

/** 后端历史消息 → 前端模型 */
export function toChatMessage(message: Message): ChatMessage {
  return {
    id: message.id,
    role: message.role,
    content: message.content,
    intent: message.intent,
    intentLabel: intentLabelOf(message.intent),
    createdAt: message.created_at,
  };
}

/** 工具卡片合并：同名且仍在 running 的卡片原位替换，否则追加 */
function mergeToolItem(
  items: TimelineItem[],
  item: Extract<TimelineItem, { kind: 'tool' }>,
): TimelineItem[] {
  const next = items.slice();
  for (let index = next.length - 1; index >= 0; index -= 1) {
    const current = next[index];
    if (
      current !== undefined &&
      current.kind === 'tool' &&
      current.tool.name === item.tool.name &&
      current.tool.status === 'running'
    ) {
      next[index] = item;
      return next;
    }
  }
  next.push(item);
  return next;
}

/** 把正在流式的一轮固化进 messages */
function finalizeTurn(
  state: ChatState,
  options: {
    messageId: string | null;
    at: string;
    error: string | null;
    escalated: boolean;
    intent: Intent | null;
  },
): ChatState {
  const live = state.live;
  if (live === null) return state;

  const hasVisibleContent = live.text.length > 0 || live.timeline.length > 0;
  if (!hasVisibleContent && options.error === null && !options.escalated) {
    // 空回合（例如被立刻取消）不留空气泡
    return { ...state, live: null };
  }

  // 优先用 intent 事件；没有的话回退到 done.intent
  const intent = live.intent ?? options.intent;

  const message: ChatMessage = {
    id: options.messageId ?? `local_${options.at}`,
    role: 'assistant',
    content: live.text,
    intent,
    intentLabel: live.intentLabel ?? intentLabelOf(intent),
    createdAt: options.at,
    timeline: live.timeline,
    kbDocs: live.kbDocs,
    slots: live.slots,
    escalated: options.escalated,
  };
  if (options.error !== null) message.error = options.error;

  return { ...state, messages: [...state.messages, message], live: null };
}

export function chatReducer(state: ChatState, action: ChatAction): ChatState {
  switch (action.type) {
    case 'history/loading':
      return { ...state, loadingHistory: true, notice: null };

    case 'history/loaded': {
      const hasHumanAgent = action.messages.some((message) => message.role === 'human_agent');
      // 静默轮询时若正在流式输出，保留本地消息，避免把乐观消息冲掉
      const keepLocalMessages = action.keepLocal && state.live !== null;
      return {
        ...state,
        sessionId: action.sessionId,
        status: action.status,
        loadingHistory: false,
        notice: null,
        messages: keepLocalMessages ? state.messages : action.messages,
        live: keepLocalMessages ? state.live : null,
        humanAgentReplied: hasHumanAgent,
        // 人工已回复 → 解除；否则保留已有转人工信息，仅在服务端状态也是 escalated 时补兜底
        escalation: hasHumanAgent
          ? null
          : action.status === 'escalated'
            ? (state.escalation ?? FALLBACK_ESCALATION)
            : state.escalation,
      };
    }

    case 'history/failed':
      return { ...state, loadingHistory: false, notice: action.message };

    case 'session/switch':
      return { ...initialState(), sessionId: action.sessionId, loadingHistory: true };

    case 'session/reset':
      return initialState();

    case 'turn/start':
      return {
        ...state,
        messages: [...state.messages, action.userMessage],
        live: emptyLiveTurn(),
        notice: null,
        feedback: null,
      };

    case 'turn/retry':
      return {
        ...state,
        messages: state.messages.slice(0, action.dropFrom),
        live: emptyLiveTurn(),
        notice: null,
      };

    case 'turn/cancel':
      return { ...state, live: null };

    case 'turn/finish': {
      const live = state.live;
      return finalizeTurn(state, {
        messageId: action.messageId,
        at: action.at,
        error: null,
        escalated: live !== null && live.escalated !== null,
        intent: action.intent,
      });
    }

    case 'sse/session':
      return { ...state, sessionId: action.sessionId };

    case 'sse/node': {
      if (state.live === null) return state;
      const item: TimelineItem = { kind: 'node', node: action.node, label: action.label };
      return { ...state, live: { ...state.live, timeline: [...state.live.timeline, item] } };
    }

    case 'sse/intent': {
      if (state.live === null) return state;
      return {
        ...state,
        live: {
          ...state.live,
          intent: action.intent,
          intentLabel: action.intentLabel,
          slots: action.slots,
        },
      };
    }

    case 'sse/kb': {
      if (state.live === null) return state;
      return { ...state, live: { ...state.live, kbDocs: action.docs } };
    }

    case 'sse/tool': {
      if (state.live === null) return state;
      return {
        ...state,
        live: { ...state.live, timeline: mergeToolItem(state.live.timeline, action.item) },
      };
    }

    case 'sse/token': {
      if (state.live === null) return state;
      return { ...state, live: { ...state.live, text: state.live.text + action.text } };
    }

    case 'sse/escalated':
      // 契约：escalated 之后本轮不再有 token。
      // 有进行中的回合就挂在它身上（气泡内说明），否则只亮横幅，避免留下不会结束的空气泡。
      return {
        ...state,
        live: state.live === null ? null : { ...state.live, escalated: action.info },
        status: 'escalated',
        escalation: action.info,
      };

    case 'sse/feedback':
      return { ...state, feedback: { messageId: action.messageId, phase: 'pending', error: null } };

    case 'sse/error': {
      if (state.live === null) return { ...state, notice: action.message };
      return finalizeTurn(state, {
        messageId: null,
        at: action.at,
        error: action.message,
        escalated: false,
        intent: null,
      });
    }

    case 'notice/dismiss':
      return { ...state, notice: null };

    case 'feedback/submitting': {
      if (state.feedback === null) return state;
      return { ...state, feedback: { ...state.feedback, phase: 'submitting', error: null } };
    }

    case 'feedback/submitted': {
      if (state.feedback === null) return state;
      return { ...state, feedback: { ...state.feedback, phase: 'done', error: null } };
    }

    case 'feedback/failed': {
      if (state.feedback === null) return state;
      return { ...state, feedback: { ...state.feedback, phase: 'pending', error: action.message } };
    }

    case 'feedback/dismiss':
      return { ...state, feedback: null };

    default:
      return state;
  }
}
