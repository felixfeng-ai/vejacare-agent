/**
 * 思考过程链（契约 5.3 / 5.4）：
 * - node 事件 → 一条可折叠的进度链
 * - tool 事件 → 链内的小卡片
 * - 流式期间默认展开并显示当前步骤，done 后自动折叠
 */

import { useEffect, useState } from 'react';

import ToolCard from './ToolCard';
import type { TimelineItem } from '../api/types';

interface ThinkingChainProps {
  items: TimelineItem[];
  /** 是否仍在流式输出中 */
  live: boolean;
}

export default function ThinkingChain({ items, live }: ThinkingChainProps) {
  const [open, setOpen] = useState(live);

  useEffect(() => {
    setOpen(live);
  }, [live]);

  if (items.length === 0) return null;

  const nodeCount = items.filter((item) => item.kind === 'node').length;
  const toolCount = items.length - nodeCount;
  const lastNode = [...items].reverse().find((item) => item.kind === 'node');

  const title = live
    ? (lastNode?.kind === 'node' ? lastNode.label : '思考中…')
    : `已完成 ${nodeCount} 步推理${toolCount > 0 ? ` · ${toolCount} 次工具调用` : ''}`;

  return (
    <div className={`think${open ? ' think--open' : ''}`}>
      <button
        type="button"
        className="think__toggle"
        onClick={() => setOpen((value) => !value)}
        aria-expanded={open}
      >
        {live ? <span className="spinner" aria-hidden="true" /> : <span className="think__done" aria-hidden="true">✓</span>}
        <span className="think__title">{title}</span>
        <span className="think__caret" aria-hidden="true">{open ? '收起' : '展开'}</span>
      </button>

      {open && (
        <ol className="think__list">
          {items.map((item, index) =>
            item.kind === 'node' ? (
              <li key={`node-${index}-${item.node}`} className="think__item">
                <span className="think__step">
                  <span className="think__dot" aria-hidden="true" />
                  {item.label}
                </span>
              </li>
            ) : (
              <li key={`tool-${index}-${item.tool.name}`} className="think__item">
                <ToolCard tool={item.tool} />
              </li>
            ),
          )}
        </ol>
      )}
    </div>
  );
}
