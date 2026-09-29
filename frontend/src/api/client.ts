/**
 * HTTP 客户端：统一信封解包 + 错误归一化。
 * 契约第 0 节：成功 { success:true, data, error:null }，失败 { success:false, data:null, error:{code,message} }。
 * 例外：/api/chat/stream 是 SSE，不走信封（见 stream.ts）。
 */

import { currentToken, notifyUnauthorized } from '../auth/session';
import { CONSOLE_ERRORS } from './types';
import type {
  ConsoleIdentity,
  ConsoleLoginInput,
  ConsoleLoginResult,
  Envelope,
  EscalationDetail,
  EscalationReplyInput,
  EscalationReplyResult,
  EscalationStatus,
  EscalationSummary,
  FeedbackInput,
  FeedbackResult,
  Metrics,
  SessionMessages,
  SessionSummary,
} from './types';

/** 未配置 VITE_API_BASE 时为空串：走相对路径，由 dev 代理 / 同源部署兜底 */
const RAW_BASE: string = (import.meta.env.VITE_API_BASE ?? '').trim();

export const API_BASE: string = RAW_BASE.replace(/\/+$/, '');

export function apiUrl(path: string): string {
  return `${API_BASE}${path}`;
}

/** 统一的接口错误；code 来自后端信封，网络层错误用自定义 code */
export class ApiError extends Error {
  readonly code: string;
  readonly status: number;

  constructor(code: string, message: string, status = 0) {
    super(message);
    this.name = 'ApiError';
    this.code = code;
    this.status = status;
  }
}

export function isAbortError(error: unknown): boolean {
  return (
    typeof error === 'object' &&
    error !== null &&
    (error as { name?: unknown }).name === 'AbortError'
  );
}

/** 把任意异常转成可展示给用户的中文文案 */
export function userMessageOf(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  if (error instanceof Error) return error.message;
  return '发生未知错误，请稍后重试';
}

function isEnvelope(value: unknown): value is Envelope<unknown> {
  if (typeof value !== 'object' || value === null) return false;
  return typeof (value as { success?: unknown }).success === 'boolean';
}

/**
 * 发起请求并解包信封：成功返回 data，失败抛 ApiError。
 */
export async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  if (!headers.has('Accept')) headers.set('Accept', 'application/json');
  if (init.body !== undefined && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json; charset=utf-8');
  }
  // 有令牌就带上。用户端接口不需要它，带了也不影响——后端不读。
  const token = currentToken();
  if (token !== null && !headers.has('Authorization')) {
    headers.set('Authorization', `Bearer ${token}`);
  }

  let response: Response;
  try {
    response = await fetch(apiUrl(path), { ...init, headers });
  } catch (error) {
    if (isAbortError(error)) throw error;
    throw new ApiError('NETWORK_ERROR', '无法连接后端服务，请确认服务已启动、基址配置正确', 0);
  }

  const text = await response.text();
  let parsed: unknown = null;
  if (text.length > 0) {
    try {
      parsed = JSON.parse(text);
    } catch {
      throw new ApiError('BAD_RESPONSE', `服务端返回了非 JSON 响应（HTTP ${response.status}）`, response.status);
    }
  }

  if (!isEnvelope(parsed)) {
    throw new ApiError('BAD_RESPONSE', `响应不符合接口信封格式（HTTP ${response.status}）`, response.status);
  }
  if (!parsed.success) {
    // 带着令牌还被判未授权 = 这个令牌死了（过期或被后端换了密钥）。
    // 清掉并广播，由 AuthProvider 把人送回登录页——比让每个页面各自处理一遍好。
    //
    // 判据是「本次请求带了令牌」而不是「状态码是 401」：登录页口令填错也是 401，
    // 那种情况没有令牌可清，也不该触发跳转（人本来就在登录页）。
    if (
      token !== null &&
      (parsed.error.code === CONSOLE_ERRORS.unauthorized ||
        parsed.error.code === CONSOLE_ERRORS.tokenInvalid)
    ) {
      notifyUnauthorized();
    }
    throw new ApiError(parsed.error.code, parsed.error.message, response.status);
  }
  // 契约保证 data 的形状；HTTP 边界无法在运行期校验，这里显式断言给调用方
  return parsed.data as T;
}

/** 契约里前端用到的全部端点 */
export const api = {
  /** 6.1 后台登录：口令换令牌 */
  login(body: ConsoleLoginInput): Promise<ConsoleLoginResult> {
    return request<ConsoleLoginResult>('/api/console/login', {
      method: 'POST',
      body: JSON.stringify(body),
    });
  },

  /** 6.2 拿令牌换身份，用于刷新页面后恢复登录态 */
  me(): Promise<ConsoleIdentity> {
    return request<ConsoleIdentity>('/api/console/me');
  },

  /** 1.3 会话列表（已按 updated_at 倒序） */
  async listSessions(): Promise<SessionSummary[]> {
    const data = await request<{ sessions: SessionSummary[] }>('/api/sessions');
    return data.sessions;
  },

  /** 1.2 拉取某个会话的历史消息 */
  listMessages(sessionId: string): Promise<SessionMessages> {
    return request<SessionMessages>(`/api/sessions/${encodeURIComponent(sessionId)}/messages`);
  },

  /** 1.4 删除会话 */
  deleteSession(sessionId: string): Promise<unknown> {
    return request<unknown>(`/api/sessions/${encodeURIComponent(sessionId)}`, { method: 'DELETE' });
  },

  /** 3.1 提交满意度评分 */
  submitFeedback(body: FeedbackInput): Promise<FeedbackResult> {
    return request<FeedbackResult>('/api/feedback', { method: 'POST', body: JSON.stringify(body) });
  },

  /** 3.2 满意度看板数据 */
  getMetrics(): Promise<Metrics> {
    return request<Metrics>('/api/metrics/satisfaction');
  },

  /** 2.1 人工工单队列（不传 status 则全部） */
  async listEscalations(status?: EscalationStatus): Promise<EscalationSummary[]> {
    const query = status === undefined ? '' : `?status=${encodeURIComponent(status)}`;
    const data = await request<{ escalations: EscalationSummary[] }>(`/api/escalations${query}`);
    return data.escalations;
  },

  /** 2.2 工单详情：额外带 AI 当时的完整上下文 */
  getEscalation(escalationId: string): Promise<EscalationDetail> {
    return request<EscalationDetail>(`/api/escalations/${encodeURIComponent(escalationId)}`);
  },

  /**
   * 2.3 人工回复。后端把回复写进会话（前端渲染成「人工客服」气泡）。
   *
   * **回一句不等于办完了**：工单仍是 pending，用户端仍在等人工、AI 仍不插话。
   * 客服可以连说几句，直到调 `closeEscalation` 结单。以前这两件事绑在一起，
   * 客服说一句「稍等，我查询下」就把工单关掉了。
   */
  replyEscalation(escalationId: string, body: EscalationReplyInput): Promise<EscalationReplyResult> {
    return request<EscalationReplyResult>(
      `/api/escalations/${encodeURIComponent(escalationId)}/reply`,
      { method: 'POST', body: JSON.stringify(body) },
    );
  },

  /**
   * 2.4 结束会话：工单结单，用户端解锁，并补一句确定性的收尾文案。
   *
   * 没回复过就直接结单，也是走这条（收尾文案为空）。后端的 `/resolve` 是同一段实现，
   * 但这里不再包一层——两条路做同一件事，界面只会用到一条，多出来的那条是死代码。
   */
  closeEscalation(escalationId: string): Promise<EscalationReplyResult> {
    return request<EscalationReplyResult>(
      `/api/escalations/${encodeURIComponent(escalationId)}/close`,
      { method: 'POST', body: JSON.stringify({}) },
    );
  },
};
