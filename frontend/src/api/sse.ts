/**
 * SSE 分帧解析（纯函数，无任何依赖，方便单独验证）。
 *
 * 为什么手写：接口是 POST，EventSource 只支持 GET，所以用 fetch + ReadableStream 自己解析。
 * 需要处理的坑：
 *   - 事件被 TCP/网络层切成两半，最后一块不完整 —— 必须留到下一轮拼接
 *   - 换行可能是 \n 也可能是 \r\n，且 \r 与 \n 有可能被切在不同 chunk
 *   - keep-alive 是注释行（以 ':' 开头）或空行，必须忽略
 *   - data: 值前面可能带一个空格
 */

/** 事件分隔符：\n\n 或 \r\n\r\n（\r 被切开时会留在 buffer 里等下一次拼接） */
const FRAME_SEPARATOR = /\r?\n\r?\n/;

export interface SplitResult {
  /** 本轮切出的完整事件块 */
  frames: string[];
  /** 尚不完整的残块，需与下一轮 chunk 拼接 */
  rest: string;
}

/**
 * 从缓冲区中切出所有「完整」事件块，余下不完整的部分原样返回。
 */
export function splitFrames(buffer: string): SplitResult {
  const frames: string[] = [];
  let rest = buffer;

  for (;;) {
    const match = FRAME_SEPARATOR.exec(rest);
    if (match === null) break;
    frames.push(rest.slice(0, match.index));
    rest = rest.slice(match.index + match[0].length);
  }

  return { frames, rest };
}

/**
 * 解析单个事件块，返回 data 载荷（多行 data 按 SSE 规范用 \n 连接）。
 * 注释行（keep-alive）或没有 data 字段的块返回 null。
 */
export function parseFrame(frame: string): string | null {
  const dataLines: string[] = [];

  for (const line of frame.split(/\r?\n/)) {
    if (line === '' || line.startsWith(':')) continue; // 空行 / keep-alive 注释
    const colon = line.indexOf(':');
    const field = colon === -1 ? line : line.slice(0, colon);
    if (field !== 'data') continue; // 只关心 data（type 在 JSON 载荷里）
    let value = colon === -1 ? '' : line.slice(colon + 1);
    if (value.startsWith(' ')) value = value.slice(1); // SSE 规范：去掉一个前导空格
    dataLines.push(value);
  }

  if (dataLines.length === 0) return null;
  return dataLines.join('\n');
}
