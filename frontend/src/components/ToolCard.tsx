/** 思考链里的工具调用小卡片：running 转圈，done 展示 summary（契约 5.4） */

import type { ToolCall } from '../api/types';

const STATUS_TEXT: Record<ToolCall['status'], string> = {
  running: '调用中',
  done: '已完成',
  error: '调用失败',
};

function prettyJson(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

export default function ToolCard({ tool }: { tool: ToolCall }) {
  const showArgs = tool.status === 'running' && tool.args !== undefined;
  const showResult = tool.status === 'error' && tool.result !== undefined;

  return (
    <div className={`tool tool--${tool.status}`}>
      <div className="tool__head">
        <code className="tool__name">{tool.name}</code>
        <span className="tool__status">
          {tool.status === 'running' && <span className="spinner" aria-hidden="true" />}
          {STATUS_TEXT[tool.status]}
        </span>
      </div>

      {tool.summary !== undefined && tool.summary.length > 0 && (
        <p className="tool__summary">{tool.summary}</p>
      )}

      {showArgs && <pre className="tool__json">{prettyJson(tool.args)}</pre>}
      {showResult && <pre className="tool__json">{prettyJson(tool.result)}</pre>}
    </div>
  );
}
