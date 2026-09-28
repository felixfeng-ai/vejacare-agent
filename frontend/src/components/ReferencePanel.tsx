/** 参考资料折叠面板（契约 5.5）：kb 事件渲染为回复下方可展开的「参考资料（N）」 */

import { useState } from 'react';

import type { KbDoc } from '../api/types';

export default function ReferencePanel({ docs }: { docs: KbDoc[] }) {
  const [open, setOpen] = useState(false);

  if (docs.length === 0) return null;

  return (
    <div className="refs">
      <button
        type="button"
        className="refs__toggle"
        onClick={() => setOpen((value) => !value)}
        aria-expanded={open}
      >
        <span>参考资料（{docs.length}）</span>
        <span className="refs__caret" aria-hidden="true">{open ? '▾' : '▸'}</span>
      </button>

      {open && (
        <ul className="refs__list">
          {docs.map((doc, index) => (
            <li key={`${doc.source}-${index}`} className="refs__item">
              <div className="refs__head">
                <span className="refs__title">{doc.title}</span>
                <span className="refs__score">相似度 {doc.score.toFixed(2)}</span>
              </div>
              <p className="refs__snippet">{doc.snippet}</p>
              <span className="refs__source">{doc.source}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
