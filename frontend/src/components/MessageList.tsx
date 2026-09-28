/**
 * 消息列表：历史消息 + 正在流式输出的那一条。
 *
 * 性能：历史气泡用 useMemo 缓存成元素数组，
 * 流式追加 token 时只有最后一个实时气泡重渲染（其余元素引用不变，React.memo 生效）。
 */

import { useEffect, useMemo, useRef } from 'react';

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
  messages: ChatMessage[];
  liveMessage: ChatMessage | null;
  loading: boolean;
  onRetry: () => void;
  onSuggestion: (text: string) => void;
}

export default function MessageList({
  messages,
  liveMessage,
  loading,
  onRetry,
  onSuggestion,
}: MessageListProps) {
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const endRef = useRef<HTMLDivElement | null>(null);

  const items = useMemo(
    () => messages.map((message) => <MessageBubble key={message.id} message={message} onRetry={onRetry} />),
    [messages, onRetry],
  );

  const liveText = liveMessage?.content ?? '';
  const liveIdle = liveMessage !== null && liveText.length === 0;

  // 新消息 / 新 token 时跟随到底部（用户主动上翻时不打扰）
  useEffect(() => {
    const container = scrollRef.current;
    if (container === null) return;
    const distance = container.scrollHeight - container.scrollTop - container.clientHeight;
    if (distance < STICK_THRESHOLD_PX) endRef.current?.scrollIntoView({ block: 'end' });
  }, [items, liveText, loading]);

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

      <div ref={endRef} />
    </div>
  );
}
