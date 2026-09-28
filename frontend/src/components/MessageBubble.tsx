/**
 * 消息气泡（契约 5.1 / 5.2 / 5.10）：
 * - user 右侧、assistant 左侧、human_agent 左侧 + 「人工客服」徽标与不同底色
 * - 流式期间显示键入光标
 * - 出错时展示错误文案 + 重试按钮
 *
 * 用 React.memo 包裹：流式追加 token 时只有实时气泡的 props 变化，历史气泡不重渲染。
 */

import { memo } from 'react';

import ReferencePanel from './ReferencePanel';
import ThinkingChain from './ThinkingChain';
import type { ChatMessage, Slots } from '../api/types';

const ROLE_LABEL: Record<ChatMessage['role'], string> = {
  user: '我',
  assistant: 'AI 客服',
  human_agent: '人工客服',
  system: '系统',
};

/** 槽位中文名，未知键直接显示原键名 */
const SLOT_LABEL: Record<string, string> = {
  order_no: '订单号',
  tracking_no: '运单号',
  sku: '商品',
};

/** 只展示有值的槽位 */
function slotChips(slots: Slots | null | undefined): Array<[string, string]> {
  if (slots == null) return [];
  return Object.entries(slots).filter(
    (entry): entry is [string, string] => typeof entry[1] === 'string' && entry[1].length > 0,
  );
}

function formatTime(iso: string): string {
  if (iso.length === 0) return '';
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return '';
  return date.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
}

export interface MessageBubbleProps {
  message: ChatMessage;
  onRetry?: (() => void) | undefined;
}

function MessageBubbleBase({ message, onRetry }: MessageBubbleProps) {
  const isUser = message.role === 'user';
  const timeline = message.timeline ?? [];
  const kbDocs = message.kbDocs ?? [];
  const streaming = message.streaming === true;
  const time = formatTime(message.createdAt);
  const slots = slotChips(message.slots);

  return (
    <article className={`row row--${message.role}`}>
      <div className={`bubble bubble--${message.role}`}>
        {!isUser && (
          <header className="bubble__head">
            <span className="bubble__role">{ROLE_LABEL[message.role]}</span>
            {message.role === 'human_agent' && <span className="badge badge--human">人工客服</span>}
            {message.intentLabel != null && <span className="badge badge--intent">{message.intentLabel}</span>}
          </header>
        )}

        {slots.length > 0 && (
          <div className="slots">
            {slots.map(([key, value]) => (
              <span key={key} className="slots__item">
                {SLOT_LABEL[key] ?? key}：{value}
              </span>
            ))}
          </div>
        )}

        {timeline.length > 0 && <ThinkingChain items={timeline} live={streaming} />}

        {message.content.length > 0 && (
          <p className="bubble__text">
            {message.content}
            {streaming && <span className="caret" aria-hidden="true" />}
          </p>
        )}

        {streaming && message.content.length === 0 && (
          <p className="bubble__text bubble__text--pending">
            <span className="caret" aria-hidden="true" />
          </p>
        )}

        {message.escalated === true && message.content.length === 0 && (
          <p className="bubble__note">本轮已转接人工客服，请稍候…</p>
        )}

        {message.error !== undefined && (
          <div className="bubble__error" role="alert">
            <span className="bubble__error-text">{message.error}</span>
            {onRetry !== undefined && (
              <button type="button" className="btn btn--ghost btn--sm" onClick={onRetry}>
                重试
              </button>
            )}
          </div>
        )}

        {kbDocs.length > 0 && <ReferencePanel docs={kbDocs} />}

        {!isUser && time.length > 0 && <time className="bubble__time">{time}</time>}
      </div>
    </article>
  );
}

export default memo(MessageBubbleBase);
