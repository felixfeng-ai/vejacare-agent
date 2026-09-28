/**
 * 会话状态机（React 胶水层）：发送消息 → 消费 SSE → 累积 token → 落库成消息。
 *
 * 纯状态逻辑在 chatReducer.ts；这里只负责：
 * 发起请求、把 SSE 事件翻译成 action、本地缓存 session_id、转人工后的历史轮询。
 */

import { useCallback, useEffect, useMemo, useReducer, useRef } from 'react';

import { api, isAbortError, userMessageOf } from '../api/client';
import { streamChat } from '../api/stream';
import {
  chatReducer,
  initialState,
  toChatMessage,
} from './chatReducer';
import type { ChatState, FeedbackState } from './chatReducer';
import type { ChatMessage, EscalationInfo, ServerEvent, SessionStatus } from '../api/types';

/** 当前会话 id 的本地缓存键（契约 5.8） */
const SESSION_STORAGE_KEY = 'vejacare.session_id';

/** 转人工后轮询历史消息，用于感知人工客服已回复 */
const ESCALATION_POLL_MS = 5000;

export type { ChatState, FeedbackState, FeedbackPhase, LiveTurn } from './chatReducer';

function readStoredSessionId(): string | null {
  try {
    return window.localStorage.getItem(SESSION_STORAGE_KEY);
  } catch {
    return null; // 隐私模式下 localStorage 不可用
  }
}

function createInitialState(): ChatState {
  const stored = readStoredSessionId();
  return { ...initialState(), sessionId: stored, loadingHistory: stored !== null };
}

export interface UseChatResult {
  sessionId: string | null;
  status: SessionStatus;
  messages: ChatMessage[];
  /** 正在流式输出的一轮（转成消息模型，便于复用同一个气泡组件） */
  liveMessage: ChatMessage | null;
  streaming: boolean;
  loadingHistory: boolean;
  notice: string | null;
  escalation: EscalationInfo | null;
  escalationActive: boolean;
  feedback: FeedbackState | null;
  /** 转人工期间锁定输入（契约 5.6） */
  inputLocked: boolean;
  send: (text: string) => Promise<void>;
  retry: () => Promise<void>;
  submitFeedback: (rating: number, comment: string) => Promise<void>;
  dismissFeedback: () => void;
  dismissNotice: () => void;
  newSession: () => void;
  selectSession: (sessionId: string) => void;
}

export function useChat(): UseChatResult {
  const [state, dispatch] = useReducer(chatReducer, undefined, createInitialState);

  // 供事件回调读取最新状态，保证 send / retry 等回调引用稳定
  const stateRef = useRef(state);
  const abortRef = useRef<AbortController | null>(null);
  const seqRef = useRef(0);

  useEffect(() => {
    stateRef.current = state;
  }, [state]);

  // 本地缓存当前 session_id（契约 5.8）
  useEffect(() => {
    try {
      if (state.sessionId === null) window.localStorage.removeItem(SESSION_STORAGE_KEY);
      else window.localStorage.setItem(SESSION_STORAGE_KEY, state.sessionId);
    } catch {
      // 忽略：缓存失败不影响功能
    }
  }, [state.sessionId]);

  const loadHistory = useCallback(async (sessionId: string, quiet = false): Promise<void> => {
    if (!quiet) dispatch({ type: 'history/loading' });
    try {
      const data = await api.listMessages(sessionId);
      dispatch({
        type: 'history/loaded',
        sessionId: data.session_id,
        status: data.status,
        messages: data.messages.map(toChatMessage),
        keepLocal: quiet,
      });
    } catch (error) {
      if (quiet) return;
      dispatch({ type: 'history/failed', message: userMessageOf(error) });
    }
  }, []);

  /** 发起一轮流式对话，并把 SSE 事件翻译成 action */
  const runStream = useCallback(async (text: string): Promise<void> => {
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;

    let finished = false;

    const handleEvent = (event: ServerEvent): void => {
      switch (event.type) {
        case 'session':
          dispatch({ type: 'sse/session', sessionId: event.session_id });
          break;
        case 'node':
          dispatch({ type: 'sse/node', node: event.node, label: event.label });
          break;
        case 'intent':
          dispatch({
            type: 'sse/intent',
            intent: event.intent,
            intentLabel: event.intent_label,
            slots: event.slots,
          });
          break;
        case 'kb':
          dispatch({ type: 'sse/kb', docs: event.docs });
          break;
        case 'tool':
          dispatch({
            type: 'sse/tool',
            item: {
              kind: 'tool',
              tool: {
                name: event.name,
                status: event.status,
                args: event.args,
                result: event.result,
                summary: event.summary,
              },
            },
          });
          break;
        case 'token':
          dispatch({ type: 'sse/token', text: event.text });
          break;
        case 'escalated':
          dispatch({
            type: 'sse/escalated',
            info: {
              reason: event.reason,
              reasonLabel: event.reason_label,
              ticketId: event.ticket_id,
            },
          });
          break;
        case 'feedback_request':
          dispatch({ type: 'sse/feedback', messageId: event.message_id });
          break;
        case 'done':
          finished = true;
          dispatch({
            type: 'turn/finish',
            messageId: event.message_id,
            at: new Date().toISOString(),
            intent: event.intent,
          });
          break;
        case 'error':
          finished = true;
          dispatch({ type: 'sse/error', message: event.message, at: new Date().toISOString() });
          break;
        default:
          break;
      }
    };

    try {
      const sessionId = stateRef.current.sessionId;
      await streamChat(
        {
          message: text,
          locale: 'zh-CN',
          ...(sessionId === null ? {} : { session_id: sessionId }),
        },
        { onEvent: handleEvent },
        controller.signal,
      );
    } catch (error) {
      if (isAbortError(error)) {
        dispatch({ type: 'turn/cancel' });
        return;
      }
      finished = true;
      dispatch({ type: 'sse/error', message: userMessageOf(error), at: new Date().toISOString() });
      return;
    }

    if (!finished) {
      // 服务端提前断开且没有 done 哨兵：收尾这一轮，避免一直转圈
      dispatch({ type: 'turn/finish', messageId: null, at: new Date().toISOString(), intent: null });
    }
  }, []);

  const send = useCallback(
    async (text: string): Promise<void> => {
      const clean = text.trim();
      if (clean.length === 0) return;
      const at = new Date().toISOString();
      seqRef.current += 1;
      dispatch({
        type: 'turn/start',
        userMessage: {
          id: `u_${at}_${seqRef.current}`,
          role: 'user',
          content: clean,
          intent: null,
          createdAt: at,
        },
      });
      await runStream(clean);
    },
    [runStream],
  );

  /** 重试：找到最后一条带错误的气泡，丢掉它并用上一条用户消息重发（契约 5.10） */
  const retry = useCallback(async (): Promise<void> => {
    const messages = stateRef.current.messages;
    let failureIndex = -1;
    for (let index = messages.length - 1; index >= 0; index -= 1) {
      const message = messages[index];
      if (message !== undefined && message.error !== undefined) {
        failureIndex = index;
        break;
      }
    }
    if (failureIndex < 0) return;

    let userText: string | null = null;
    for (let index = failureIndex - 1; index >= 0; index -= 1) {
      const message = messages[index];
      if (message !== undefined && message.role === 'user') {
        userText = message.content;
        break;
      }
    }
    if (userText === null) return;

    dispatch({ type: 'turn/retry', dropFrom: failureIndex });
    await runStream(userText);
  }, [runStream]);

  const submitFeedback = useCallback(async (rating: number, comment: string): Promise<void> => {
    const current = stateRef.current;
    const feedback = current.feedback;
    const sessionId = current.sessionId;
    if (feedback === null) return;
    if (sessionId === null) {
      dispatch({ type: 'feedback/failed', message: '会话尚未建立，暂时无法提交评价' });
      return;
    }

    dispatch({ type: 'feedback/submitting' });
    try {
      const trimmed = comment.trim();
      await api.submitFeedback({
        session_id: sessionId,
        message_id: feedback.messageId,
        rating,
        ...(trimmed.length === 0 ? {} : { comment: trimmed }),
      });
      dispatch({ type: 'feedback/submitted' });
    } catch (error) {
      dispatch({ type: 'feedback/failed', message: userMessageOf(error) });
    }
  }, []);

  const dismissFeedback = useCallback((): void => {
    dispatch({ type: 'feedback/dismiss' });
  }, []);

  const dismissNotice = useCallback((): void => {
    dispatch({ type: 'notice/dismiss' });
  }, []);

  const newSession = useCallback((): void => {
    abortRef.current?.abort();
    dispatch({ type: 'session/reset' });
  }, []);

  const selectSession = useCallback(
    (sessionId: string): void => {
      abortRef.current?.abort();
      dispatch({ type: 'session/switch', sessionId });
      void loadHistory(sessionId);
    },
    [loadHistory],
  );

  // 首次进入：有本地缓存的 session_id 就把历史拉回来
  useEffect(() => {
    const stored = stateRef.current.sessionId;
    if (stored !== null) void loadHistory(stored);
  }, [loadHistory]);

  const escalationActive = state.escalation !== null && !state.humanAgentReplied;

  // 转人工后轮询历史，感知人工客服回复（契约 5.6）
  useEffect(() => {
    if (!escalationActive) return undefined;
    const sessionId = state.sessionId;
    if (sessionId === null) return undefined;

    const timer = window.setInterval(() => {
      if (stateRef.current.live !== null) return; // 本轮还没结束，下一轮再同步
      void loadHistory(sessionId, true);
    }, ESCALATION_POLL_MS);

    return () => {
      window.clearInterval(timer);
    };
  }, [escalationActive, state.sessionId, loadHistory]);

  // 把正在流式的一轮转成消息模型，复用同一个气泡组件
  const liveMessage = useMemo<ChatMessage | null>(() => {
    const live = state.live;
    if (live === null) return null;
    return {
      id: '__live__',
      role: 'assistant',
      content: live.text,
      intent: live.intent,
      intentLabel: live.intentLabel,
      createdAt: '',
      timeline: live.timeline,
      kbDocs: live.kbDocs,
      slots: live.slots,
      escalated: live.escalated !== null,
      streaming: true,
    };
  }, [state.live]);

  return {
    sessionId: state.sessionId,
    status: state.status,
    messages: state.messages,
    liveMessage,
    streaming: state.live !== null,
    loadingHistory: state.loadingHistory,
    notice: state.notice,
    escalation: state.escalation,
    escalationActive,
    feedback: state.feedback,
    inputLocked: escalationActive,
    send,
    retry,
    submitFeedback,
    dismissFeedback,
    dismissNotice,
    newSession,
    selectSession,
  };
}
