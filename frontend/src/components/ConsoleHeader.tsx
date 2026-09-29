/** 后台页面的公共页头：这是谁、能去哪、怎么退出 */

import type { ReactNode } from 'react';
import { Link, useLocation, useNavigate } from 'react-router-dom';

import { useAuth } from '../auth/AuthContext';
import { CONSOLE_ROLE_LABELS } from '../api/types';

export default function ConsoleHeader({
  title,
  badge,
  onRefresh,
  refreshing = false,
  children,
}: {
  title: string;
  /** 标题旁的小标记，例如「3 张待处理」 */
  badge?: string;
  onRefresh?: () => void;
  refreshing?: boolean;
  children?: ReactNode;
}) {
  const { name, role, logout } = useAuth();
  const navigate = useNavigate();
  const { pathname } = useLocation();

  return (
    <header className="console__head">
      <div className="console__title">
        <h1 className="console__name">
          {title}
          {badge !== undefined && <span className="desk__badge">{badge}</span>}
        </h1>
        <span className="console__who">
          {name}
          {role !== null && ` · ${CONSOLE_ROLE_LABELS[role]}`}
        </span>
      </div>

      <div className="console__tools">
        {children}
        {onRefresh !== undefined && (
          <button
            type="button"
            className="btn btn--ghost btn--sm"
            onClick={onRefresh}
            disabled={refreshing}
          >
            刷新
          </button>
        )}
        {/* 只在管理者眼里出现：客服没有看板权限，给他一个点了就 403 的链接是耍人。
            另外别在工单台页面上再链一次工单台——那是原地打转 */}
        {role === 'admin' && pathname !== '/desk' && (
          <Link className="btn btn--ghost btn--sm" to="/desk">
            客服工单
          </Link>
        )}
        <Link className="btn btn--ghost btn--sm" to="/chat">
          返回用户端
        </Link>
        <button
          type="button"
          className="btn btn--ghost btn--sm"
          onClick={() => {
            logout();
            navigate('/login', { replace: true });
          }}
        >
          退出登录
        </button>
      </div>
    </header>
  );
}
