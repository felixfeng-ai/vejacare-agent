/**
 * 满意度评分卡（契约 5.7）：feedback_request 事件后弹出 1–5 星 + 备注，
 * 提交成功后收起并显示「感谢您的评价」。
 */

import { useState } from 'react';

import type { FeedbackState } from '../hooks/useChat';

const RATING_TEXT: Record<number, string> = {
  1: '很不满意',
  2: '不太满意',
  3: '一般',
  4: '比较满意',
  5: '非常满意',
};

const STARS = [1, 2, 3, 4, 5] as const;

export interface FeedbackCardProps {
  feedback: FeedbackState;
  onSubmit: (rating: number, comment: string) => void;
  onDismiss: () => void;
}

export default function FeedbackCard({ feedback, onSubmit, onDismiss }: FeedbackCardProps) {
  const [rating, setRating] = useState(0);
  const [comment, setComment] = useState('');

  if (feedback.phase === 'done') {
    return (
      <div className="card card--feedback" role="status">
        <p className="card__thanks">感谢您的评价 🌟</p>
        <button type="button" className="btn btn--ghost btn--sm" onClick={onDismiss}>
          关闭
        </button>
      </div>
    );
  }

  const submitting = feedback.phase === 'submitting';
  const canSubmit = rating > 0 && !submitting;

  return (
    <section className="card card--feedback" aria-label="满意度评价">
      <header className="card__head">
        <h3 className="card__title">这次服务您满意吗？</h3>
        <button type="button" className="card__close" onClick={onDismiss} aria-label="暂不评价">
          ×
        </button>
      </header>

      <div className="stars" role="radiogroup" aria-label="评分">
        {STARS.map((value) => (
          <button
            key={value}
            type="button"
            role="radio"
            aria-checked={rating === value}
            aria-label={`${value} 星`}
            className={`star${value <= rating ? ' star--on' : ''}`}
            onClick={() => setRating(value)}
            disabled={submitting}
          >
            ★
          </button>
        ))}
        {rating > 0 && <span className="stars__text">{RATING_TEXT[rating]}</span>}
      </div>

      <textarea
        className="card__comment"
        value={comment}
        rows={2}
        placeholder="补充说明（可选）"
        onChange={(event) => setComment(event.target.value)}
        disabled={submitting}
        aria-label="评价备注"
      />

      {feedback.error !== null && <p className="card__error">{feedback.error}</p>}

      <div className="card__actions">
        <button type="button" className="btn btn--ghost btn--sm" onClick={onDismiss} disabled={submitting}>
          暂不评价
        </button>
        <button
          type="button"
          className="btn btn--primary btn--sm"
          onClick={() => onSubmit(rating, comment)}
          disabled={!canSubmit}
        >
          {submitting ? '提交中…' : '提交评价'}
        </button>
      </div>
    </section>
  );
}
