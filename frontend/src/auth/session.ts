/**
 * 后台登录态的存取与广播。
 *
 * 令牌存在 localStorage（键见 SESSION_KEY）。这个选择有代价：XSS 能把它读走。
 * 换来的是不必引 httpOnly cookie 所需的 CSRF 防护与跨域配置。取舍写在
 * specs/001 §10，真要上生产应当换成 httpOnly cookie。
 *
 * 整份登录态存成一个 JSON、放在一个键里，而不是拆成三个键（token/role/name）：
 * 拆开就可能出现「有角色没令牌」这种半截状态，读的人还得各自处理。
 */

import type { ConsoleIdentity, ConsoleRole } from '../api/types';

const SESSION_KEY = 'veyacare.console_session';

export interface ConsoleSession extends ConsoleIdentity {
  token: string;
}

function isRole(value: unknown): value is ConsoleRole {
  return value === 'agent' || value === 'admin';
}

function parse(raw: string): ConsoleSession | null {
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return null;
  }
  if (typeof parsed !== 'object' || parsed === null) return null;

  const candidate = parsed as Partial<ConsoleSession>;
  if (typeof candidate.token !== 'string' || candidate.token.length === 0) return null;
  if (!isRole(candidate.role)) return null;
  if (typeof candidate.expires_at !== 'number') return null;
  return {
    token: candidate.token,
    role: candidate.role,
    name: typeof candidate.name === 'string' ? candidate.name : '',
    expires_at: candidate.expires_at,
  };
}

/**
 * 读取登录态。**过期即视为没有**——本地判一次过期，省掉一次注定 401 的请求。
 * 这只是省流量：真正的判据在后端，改本地时间骗不过签名。
 */
export function readSession(): ConsoleSession | null {
  let raw: string | null;
  try {
    raw = window.localStorage.getItem(SESSION_KEY);
  } catch {
    // 隐私模式 / 禁用存储：当作未登录，页面照常能打开，只是进不去后台
    return null;
  }
  if (raw === null) return null;

  const session = parse(raw);
  if (session === null) {
    clearSession();
    return null;
  }
  if (session.expires_at <= Math.floor(Date.now() / 1000)) {
    clearSession();
    return null;
  }
  return session;
}

export function saveSession(session: ConsoleSession): void {
  try {
    window.localStorage.setItem(SESSION_KEY, JSON.stringify(session));
  } catch {
    // 存不下就只在内存里活着，刷新即失效——比抛异常把登录页打崩好
  }
}

export function clearSession(): void {
  try {
    window.localStorage.removeItem(SESSION_KEY);
  } catch {
    // 同上，清不掉也不该让调用方崩
  }
}

/** 当前令牌，供 request() 自动附加 */
export function currentToken(): string | null {
  return readSession()?.token ?? null;
}

// ------------------------------------------------------------------ 未授权广播

type Listener = () => void;

const listeners = new Set<Listener>();

/**
 * 订阅「令牌失效」。
 *
 * 为什么用广播而不是在 client.ts 里直接跳转：client 是纯网络层，让它知道路由
 * 就把两件事绑死了；而且它手上没有 React 的导航能力，只能 window.location 硬跳，
 * 那是一次整页刷新。广播交给 AuthProvider 处理，才能走正常的路由切换。
 */
export function onUnauthorized(listener: Listener): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

export function notifyUnauthorized(): void {
  clearSession();
  for (const listener of listeners) listener();
}
