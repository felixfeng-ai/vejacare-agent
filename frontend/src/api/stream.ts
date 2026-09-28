/**
 * POST /api/chat/stream 的流式客户端：fetch + ReadableStream 手写 SSE 解析。
 */

import { ApiError, apiUrl, isAbortError } from './client';
import { parseFrame, splitFrames } from './sse';
import { isServerEvent } from './types';
import type { ServerEvent } from './types';

export interface StreamChatBody {
  /** 可选；不传则后端新建，并在 session 事件里返回 */
  session_id?: string;
  message: string;
  /** 可选，默认 zh-CN */
  locale?: string;
}

export interface StreamCallbacks {
  /** 每个合法事件回调一次，调用方按 evt.type 分发 */
  onEvent: (event: ServerEvent) => void;
  /** 单个块解析失败：整条流不中断，仅上报（便于排查后端异常帧） */
  onParseError?: (raw: string, error: unknown) => void;
}

/** 读取非 2xx 响应，尽量还原后端信封里的错误码与文案 */
async function readErrorEnvelope(response: Response): Promise<ApiError> {
  let code = `HTTP_${response.status}`;
  let message = `请求失败（HTTP ${response.status}）`;

  try {
    const text = await response.text();
    if (text.length > 0) {
      const parsed: unknown = JSON.parse(text);
      if (typeof parsed === 'object' && parsed !== null) {
        const error = (parsed as { error?: unknown }).error;
        if (typeof error === 'object' && error !== null) {
          const payload = error as { code?: unknown; message?: unknown };
          if (typeof payload.code === 'string') code = payload.code;
          if (typeof payload.message === 'string') message = payload.message;
        }
      }
    }
  } catch {
    // 后端未返回 JSON，保留默认文案
  }

  return new ApiError(code, message, response.status);
}

/** 解析并派发一个事件块；解析失败不抛出，避免一个坏帧打断整轮对话 */
function emitFrame(frame: string, callbacks: StreamCallbacks): void {
  const payload = parseFrame(frame);
  if (payload === null || payload.length === 0) return; // 纯 keep-alive

  let parsed: unknown;
  try {
    parsed = JSON.parse(payload);
  } catch (error) {
    callbacks.onParseError?.(payload, error);
    return;
  }

  if (!isServerEvent(parsed)) {
    callbacks.onParseError?.(payload, new Error('未知的 SSE 事件类型'));
    return;
  }

  callbacks.onEvent(parsed);
}

/**
 * 发起流式对话。
 * - 正常结束时 resolve；
 * - HTTP/网络错误抛 ApiError；
 * - 主动 abort（signal）时抛出 AbortError，调用方应忽略。
 */
export async function streamChat(
  body: StreamChatBody,
  callbacks: StreamCallbacks,
  signal?: AbortSignal,
): Promise<void> {
  let response: Response;
  try {
    response = await fetch(apiUrl('/api/chat/stream'), {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json; charset=utf-8',
        Accept: 'text/event-stream',
      },
      body: JSON.stringify(body),
      ...(signal ? { signal } : {}),
    });
  } catch (error) {
    if (isAbortError(error)) throw error;
    throw new ApiError('NETWORK_ERROR', '无法连接后端服务，请确认服务已启动、基址配置正确', 0);
  }

  if (!response.ok) throw await readErrorEnvelope(response);
  if (response.body === null) {
    throw new ApiError('NO_STREAM_BODY', '当前浏览器不支持流式响应（response.body 为空）', response.status);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder('utf-8');
  let buffer = '';

  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;

      // stream: true —— 保留跨 chunk 的半个多字节字符
      buffer += decoder.decode(value, { stream: true });

      const { frames, rest } = splitFrames(buffer);
      buffer = rest;
      for (const frame of frames) emitFrame(frame, callbacks);
    }

    // 收尾：解码残字节；有的服务端最后一块事件不带结尾空行
    buffer += decoder.decode();
    const { frames, rest } = splitFrames(buffer);
    for (const frame of frames) emitFrame(frame, callbacks);
    if (rest.trim().length > 0) emitFrame(rest, callbacks);
  } finally {
    reader.releaseLock();
  }
}
