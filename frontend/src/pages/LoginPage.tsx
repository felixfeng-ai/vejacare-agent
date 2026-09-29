/**
 * 后台登录页：口令换令牌。
 *
 * 一个输入框，没有用户名。后台用的是**共享口令**（客服一套、管理一套），
 * 换来的能力是「你是谁」这个问题不重要——只要回答「你能干什么」。
 * 代价是没法追责到具体某个人，署名只是缓解手段，不是审计（见 specs/001 §10）。
 */

import { useCallback, useEffect, useState } from 'react';
import { Link, useLocation, useNavigate } from 'react-router-dom';

import { useAuth } from '../auth/AuthContext';
import { userMessageOf } from '../api/client';
import { CONSOLE_ERRORS } from '../api/types';

/** 登录成功后要跳去的地方。守卫把来路塞在 location.state 里。 */
interface FromState {
  from?: string;
}

export default function LoginPage() {
  const { login, session, role, ready } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();

  const [password, setPassword] = useState('');
  const [agentName, setAgentName] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const from = (location.state as FromState | null)?.from;

  // 已登录的人不该停在登录页。管理员默认去看板，客服默认去工单台
  useEffect(() => {
    if (!ready || session === null) return;
    const fallback = role === 'admin' ? '/admin' : '/desk';
    navigate(from ?? fallback, { replace: true });
  }, [from, navigate, ready, role, session]);

  const handleSubmit = useCallback(async (): Promise<void> => {
    if (password.length === 0) return;
    setBusy(true);
    setError(null);
    try {
      await login(password, agentName.trim() === '' ? undefined : agentName.trim());
      // 跳转交给上面的 useEffect——它同时覆盖「进来时就已经登录」的情况
    } catch (err) {
      const code = (err as { code?: string }).code;
      if (code === CONSOLE_ERRORS.disabled) {
        // 这不是「你口令错了」，是服务端根本没启用后台。提示要能指向真正的处理办法，
        // 否则运维会一直在这里试口令
        setError('服务端未配置后台访问口令，功能未启用。请联系运维配置 CONSOLE_* 后重试。');
      } else {
        setError(userMessageOf(err));
      }
    } finally {
      setBusy(false);
    }
  }, [agentName, login, password]);

  return (
    <div className="page page--center">
      <form
        className="card card--narrow"
        onSubmit={(event) => {
          event.preventDefault();
          void handleSubmit();
        }}
      >
        <h1 className="card__title">VeyaCare 后台</h1>
        <p className="page__hint">客服工作台与经营看板只对内部开放。</p>

        <label className="field__label" htmlFor="login-password">
          访问口令
        </label>
        <input
          id="login-password"
          className="field__input"
          type="password"
          value={password}
          autoComplete="current-password"
          autoFocus
          onChange={(event) => setPassword(event.target.value)}
        />

        <label className="field__label" htmlFor="login-name">
          客服署名（可选）
        </label>
        <input
          id="login-name"
          className="field__input"
          value={agentName}
          maxLength={32}
          placeholder="回复工单时显示给用户看的名字"
          onChange={(event) => setAgentName(event.target.value)}
        />

        {error !== null && (
          <p className="card__error" role="alert">
            {error}
          </p>
        )}

        <button
          type="submit"
          className="btn btn--primary"
          disabled={busy || password.length === 0}
        >
          {busy ? '登录中…' : '登录'}
        </button>

        <Link className="page__back" to="/chat">
          ← 返回用户端
        </Link>
      </form>
    </div>
  );
}
