/** 转人工横幅（契约 5.6）：escalated 事件后出现在输入框上方，并锁定输入 */

import type { EscalationInfo } from '../api/types';

export default function EscalationBanner({ info }: { info: EscalationInfo }) {
  return (
    <div className="banner banner--escalation" role="status">
      <span className="banner__icon" aria-hidden="true">🎧</span>
      <div className="banner__body">
        <p className="banner__title">已为您转接人工客服</p>
        <p className="banner__desc">
          {info.reasonLabel}
          {info.ticketId !== null && <span className="banner__ticket">工单号 {info.ticketId}</span>}
          <span className="banner__hint">人工回复后即可继续对话</span>
        </p>
      </div>
    </div>
  );
}
