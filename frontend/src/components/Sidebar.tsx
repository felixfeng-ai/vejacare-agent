/**
 * 左侧历史会话栏（契约 5.8）：列出 /api/sessions，可切换、可删除。
 * ≤768px 时变成抽屉（由 CSS 控制位移，open 控制展开）。
 */

import type { SessionStatus, SessionSummary } from '../api/types';

const STATUS_LABEL: Record<SessionStatus, string> = {
  active: '进行中',
  escalated: '待人工',
  closed: '已结束',
};

function formatUpdatedAt(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return '';
  const now = new Date();
  const sameDay =
    date.getFullYear() === now.getFullYear() &&
    date.getMonth() === now.getMonth() &&
    date.getDate() === now.getDate();
  return sameDay
    ? date.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
    : date.toLocaleDateString('zh-CN', { month: '2-digit', day: '2-digit' });
}

export interface SidebarProps {
  sessions: SessionSummary[];
  currentId: string | null;
  loading: boolean;
  error: string | null;
  removingId: string | null;
  open: boolean;
  onClose: () => void;
  onSelect: (sessionId: string) => void;
  onDelete: (sessionId: string) => void;
  onNew: () => void;
  onRefresh: () => void;
  onOpenMetrics: () => void;
}

export default function Sidebar({
  sessions,
  currentId,
  loading,
  error,
  removingId,
  open,
  onClose,
  onSelect,
  onDelete,
  onNew,
  onRefresh,
  onOpenMetrics,
}: SidebarProps) {
  return (
    <>
      {open && <div className="sidebar__mask" onClick={onClose} aria-hidden="true" />}

      <aside className={`sidebar${open ? ' sidebar--open' : ''}`} aria-label="历史会话">
        <header className="sidebar__head">
          <div className="brand">
            <span className="brand__mark" aria-hidden="true">V</span>
            <div className="brand__text">
              <strong className="brand__name">VeyaCare</strong>
              <span className="brand__sub">跨境智能客服</span>
            </div>
          </div>
          <button type="button" className="sidebar__close" onClick={onClose} aria-label="收起侧栏">
            ×
          </button>
        </header>

        <button type="button" className="btn btn--primary sidebar__new" onClick={onNew}>
          ＋ 新建会话
        </button>

        <div className="sidebar__section">
          <span className="sidebar__section-title">历史会话</span>
          <button
            type="button"
            className="btn btn--ghost btn--sm"
            onClick={onRefresh}
            disabled={loading}
          >
            {loading ? '刷新中…' : '刷新'}
          </button>
        </div>

        {error !== null && <p className="sidebar__error">{error}</p>}

        <nav className="sessions">
          {!loading && sessions.length === 0 && error === null && (
            <p className="sidebar__hint">还没有会话，发一条消息就开始啦。</p>
          )}

          <ul className="sessions__list">
            {sessions.map((session) => (
              <li key={session.session_id}>
                <div
                  className={`session${session.session_id === currentId ? ' session--active' : ''}`}
                >
                  <button
                    type="button"
                    className="session__main"
                    onClick={() => {
                      onSelect(session.session_id);
                      onClose();
                    }}
                    title={session.title}
                  >
                    <span className="session__title">
                      {session.title.length > 0 ? session.title : '未命名会话'}
                    </span>
                    <span className="session__meta">
                      <span className={`session__status session__status--${session.status}`}>
                        {STATUS_LABEL[session.status]}
                      </span>
                      <span className="session__time">{formatUpdatedAt(session.updated_at)}</span>
                      <span className="session__count">{session.message_count} 条</span>
                    </span>
                  </button>
                  <button
                    type="button"
                    className="session__delete"
                    aria-label="删除会话"
                    disabled={removingId === session.session_id}
                    onClick={() => {
                      if (window.confirm('确定删除这个会话吗？删除后不可恢复。')) {
                        onDelete(session.session_id);
                      }
                    }}
                  >
                    🗑
                  </button>
                </div>
              </li>
            ))}
          </ul>
        </nav>

        <footer className="sidebar__foot">
          <button type="button" className="btn btn--ghost sidebar__metrics" onClick={onOpenMetrics}>
            📊 数据看板
          </button>
        </footer>
      </aside>
    </>
  );
}
