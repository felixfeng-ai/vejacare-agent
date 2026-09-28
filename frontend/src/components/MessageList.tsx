/**
 * 消息列表：历史消息 + 正在流式输出的那一条。
 *
 * 性能：历史气泡用 useMemo 缓存成元素数组，
 * 流式追加 token 时只有最后一个实时气泡重渲染（其余元素引用不变，React.memo 生效）。
 */

import { useLayoutEffect, useMemo, useRef } from 'react';

import MessageBubble from './MessageBubble';
import type { ChatMessage } from '../api/types';

/** 底部跟随阈值：距底部超过该距离就认为用户在翻阅历史，不再自动滚动 */
const STICK_THRESHOLD_PX = 120;

const SUGGESTIONS: string[] = [
  '我的包裹到哪了？订单号 SO20260928001',
  '关税大概怎么算？',
  '这个尺码偏小吗？',
  '我想申请退货',
];

export interface MessageListProps {
  /** 当前会话 id，换会话时用来重新贴底 */
  sessionId: string | null;
  messages: ChatMessage[];
  liveMessage: ChatMessage | null;
  loading: boolean;
  onRetry: () => void;
  onSuggestion: (text: string) => void;
}

export default function MessageList({
  sessionId,
  messages,
  liveMessage,
  loading,
  onRetry,
  onSuggestion,
}: MessageListProps) {
  const scrollRef = useRef<HTMLDivElement | null>(null);

  /**
   * 上一次提交时内容有多高。
   *
   * 判断"要不要贴底"，必须拿**上一次**的高度来量，把这一次长高的部分减掉，
   * 量出来的才是"用户自己滚动留下的位置"。
   *
   * 换成就地量 `scrollHeight - scrollTop - clientHeight` 是不行的：那是拿长高
   * **之后**的高度在量，于是一次渲染长高超过 120 像素就会被误判成"用户上翻了"——
   * 思考链与工具卡正是一次性渲染出来的（一次几百像素），每轮回答开头都要踩一次；
   * 而内容只会继续变高，距离再也回不到阈值以内，跟随就被**永久**关掉，
   * 整段回答都停在原地。这个误判只在"先加载过一段历史、再提问"时出现，
   * 所以看起来像是偶发。
   *
   * 也试过"用滚动事件记录用户意图"：滚动事件是排队派发的，我们自己的贴底写入会
   * 先执行、用户的上翻事件后处理，处理器读到的已经是贴底后的位置，于是把用户的
   * 上翻当成"他在底部"，回答继续往下跑，人却被留在上面。
   */
  const prevHeightRef = useRef(0);

  const items = useMemo(
    () => messages.map((message) => <MessageBubble key={message.id} message={message} onRetry={onRetry} />),
    [messages, onRetry],
  );

  const liveText = liveMessage?.content ?? '';
  const liveIdle = liveMessage !== null && liveText.length === 0;

  const prevSessionRef = useRef(sessionId);
  const prevLiveRef = useRef(liveMessage !== null);

  /**
   * 贴底。用 useLayoutEffect 而不是 useEffect：滚动位置的调整必须在浏览器绘制前完成，
   * 否则会看到一次"停在原地再跳下去"的抖动。
   *
   * 强制贴底只认两种**边沿**：换会话、以及用户刚发出问题（live 由无变有）。
   * 反向的边沿（回答结束、live 变回 null）不能强制 —— 那时用户可能正上翻看历史，
   * 拽他回底部是最招人烦的一种"帮忙"。
   */
  useLayoutEffect(() => {
    const container = scrollRef.current;
    if (container === null) return;

    const height = container.scrollHeight;
    const prevHeight = prevHeightRef.current;
    prevHeightRef.current = height;

    const sessionChanged = prevSessionRef.current !== sessionId;
    const liveStarted = liveMessage !== null && !prevLiveRef.current;
    prevSessionRef.current = sessionId;
    prevLiveRef.current = liveMessage !== null;

    const userDistance = prevHeight - container.scrollTop - container.clientHeight;
    if (!sessionChanged && !liveStarted && userDistance >= STICK_THRESHOLD_PX) return;
    container.scrollTop = height;
  }, [items, liveText, loading, sessionId, liveMessage]);

  const isEmpty = messages.length === 0 && liveMessage === null;

  return (
    <div className="messages" ref={scrollRef}>
      {loading && <p className="messages__hint">正在加载历史消息…</p>}

      {!loading && isEmpty && (
        <div className="welcome">
          <h2 className="welcome__title">你好，我是 VeyaCare 智能客服</h2>
          <p className="welcome__desc">
            可以帮你查物流、处理退换货、算关税、选尺码。直接说出你的问题，或者点下面的例子试试。
          </p>
          <div className="welcome__chips">
            {SUGGESTIONS.map((text) => (
              <button
                key={text}
                type="button"
                className="chip"
                onClick={() => onSuggestion(text)}
              >
                {text}
              </button>
            ))}
          </div>
        </div>
      )}

      {items}
      {liveMessage !== null && (
        <MessageBubble key="__live__" message={liveMessage} onRetry={onRetry} />
      )}
      {liveIdle && <span className="sr-only" role="status">AI 正在输入</span>}
    </div>
  );
}
