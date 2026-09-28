/**
 * HTTP 客户端：统一信封解包 + 错误归一化。
 * 契约第 0 节：成功 { success:true, data, error:null }，失败 { success:false, data:null, error:{code,message} }。
 * 例外：/api/chat/stream 是 SSE，不走信封（见 stream.ts）。
 */

import type {
  Envelope,
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
    throw new ApiError(parsed.error.code, parsed.error.message, response.status);
  }
  // 契约保证 data 的形状；HTTP 边界无法在运行期校验，这里显式断言给调用方
  return parsed.data as T;
}

/** 契约里前端用到的全部端点 */
export const api = {
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
};
