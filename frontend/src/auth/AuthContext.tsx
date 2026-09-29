/**
 * 登录态上下文：全应用唯一的「现在是谁」。
 *
 * 为什么需要一个 Provider 而不是各处直接读 localStorage：
 * 一是 localStorage 是外部状态，改了不会触发 React 重渲染，登录/登出后界面不刷新；
 * 二是令牌失效要能被广播到一处统一处理（跳登录页），而不是十个页面各写一遍。
 */

import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';
import type { ReactNode } from 'react';

import { api } from '../api/client';
import type { ConsoleRole } from '../api/types';
import { clearSession, onUnauthorized, readSession, saveSession } from './session';
import type { ConsoleSession } from './session';

interface AuthValue {
  /** 当前登录态；null = 未登录 */
  session: ConsoleSession | null;
  role: ConsoleRole | null;
  name: string;
  /**
   * 是否已确认过登录态。
   *
   * 守卫必须等它变 true 再决定放不放行：本地有令牌不等于令牌有效，
   * 不等校验直接放行的话，令牌过期时页面会先闪一下后台界面再跳登录页。
   */
  ready: boolean;
  login: (password: string, name?: string) => Promise<void>;
  logout: () => void;
}

const AuthContext = createContext<AuthValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<ConsoleSession | null>(readSession);
  const [ready, setReady] = useState(false);

  // 令牌失效（401）时由 client 层广播过来，统一清态
  useEffect(() => {
    return onUnauthorized(() => {
      setSession(null);
      setReady(true);
    });
  }, []);

  // 挂载时拿令牌问一次后端。本地判断只看得到过期时间，看不到「后端换了密钥」
  // 或「后台被关掉」这类情况——那些只有真打一次接口才知道。
  useEffect(() => {
    if (readSession() === null) {
      setReady(true);
      return;
    }
    let cancelled = false;
    void (async () => {
      try {
        const identity = await api.me();
        if (cancelled) return;
        setSession((current) => {
          if (current === null) return null;
          const next: ConsoleSession = { ...current, ...identity };
          saveSession(next);
          return next;
        });
      } catch {
        // 401 已经由 client 广播清掉了；503（后台未配置）留给页面自己提示，
        // 这里只管把 ready 打开，别把界面永远卡在加载中
        if (!cancelled) setSession(readSession());
      } finally {
        if (!cancelled) setReady(true);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const login = useCallback(async (password: string, name?: string): Promise<void> => {
    const result = await api.login(name === undefined ? { password } : { password, name });
    const next: ConsoleSession = {
      token: result.token,
      role: result.role,
      name: result.name,
      expires_at: result.expires_at,
    };
    saveSession(next);
    setSession(next);
  }, []);

  const logout = useCallback((): void => {
    clearSession();
    setSession(null);
  }, []);

  const value = useMemo<AuthValue>(
    () => ({
      session,
      role: session?.role ?? null,
      name: session?.name ?? '',
      ready,
      login,
      logout,
    }),
    [login, logout, ready, session],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthValue {
  const value = useContext(AuthContext);
  if (value === null) {
    throw new Error('useAuth 必须在 <AuthProvider> 内使用');
  }
  return value;
}
