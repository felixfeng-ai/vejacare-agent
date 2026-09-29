/**
 * 路由守卫：没登录赶去登录页，角色不对就地告知。
 *
 * 注意这只是界面层的门——真正的门在后端（401/403）。前端守卫挡的是"误入"，
 * 挡不住"绕过"，所以后端一个依赖都不能省。
 */

import type { ReactNode } from 'react';
import { Navigate, useLocation } from 'react-router-dom';

import { useAuth } from '../auth/AuthContext';
import { CONSOLE_ROLE_LABELS } from '../api/types';
import type { ConsoleRole } from '../api/types';

export default function RequireRole({
  roles,
  children,
}: {
  roles: readonly ConsoleRole[];
  children: ReactNode;
}) {
  const { session, role, ready } = useAuth();
  const location = useLocation();

  // 还在校验令牌，先什么都不渲染——避免后台界面闪一下再跳走
  if (!ready) {
    return <p className="page__hint">正在校验登录状态…</p>;
  }

  if (session === null || role === null) {
    // 把来路记下来，登录后回到原来想去的地方，而不是一律回首页
    return <Navigate to="/login" replace state={{ from: location.pathname }} />;
  }

  if (!roles.includes(role)) {
    const allowed = roles.map((item) => CONSOLE_ROLE_LABELS[item]).join(' / ');
    return (
      <div className="page">
        <div className="card">
          <h2 className="card__title">无权访问</h2>
          <p className="card__error" role="alert">
            当前身份是「{CONSOLE_ROLE_LABELS[role]}」，这个页面只对「{allowed}」开放。
          </p>
          <p className="page__hint">
            客服看工单、管理者看经营看板，是两套不同的权限。需要更高权限请用对应口令重新登录。
          </p>
        </div>
      </div>
    );
  }

  return <>{children}</>;
}
