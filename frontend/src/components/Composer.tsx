/** 输入框：Enter 发送、Shift+Enter 换行；转人工期间禁用（契约 5.6） */

import { useCallback, useEffect, useRef, useState } from 'react';
import type { KeyboardEvent } from 'react';

const MAX_HEIGHT_PX = 160;

export interface ComposerProps {
  /** 整体禁用（转人工锁定） */
  disabled: boolean;
  disabledHint: string | null;
  streaming: boolean;
  onSend: (text: string) => void;
}

export default function Composer({ disabled, disabledHint, streaming, onSend }: ComposerProps) {
  const [text, setText] = useState('');
  const areaRef = useRef<HTMLTextAreaElement | null>(null);

  // 随内容自增高
  useEffect(() => {
    const area = areaRef.current;
    if (area === null) return;
    area.style.height = 'auto';
    area.style.height = `${Math.min(area.scrollHeight, MAX_HEIGHT_PX)}px`;
  }, [text]);

  const submit = useCallback((): void => {
    const clean = text.trim();
    if (clean.length === 0 || disabled || streaming) return;
    onSend(clean);
    setText('');
  }, [disabled, onSend, streaming, text]);

  const handleKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>): void => {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      submit();
    }
  };

  const canSend = text.trim().length > 0 && !disabled && !streaming;

  const placeholder = disabled
    ? (disabledHint ?? '当前无法发送消息')
    : '输入你的问题，Enter 发送 / Shift+Enter 换行';

  return (
    <form
      className="composer"
      onSubmit={(event) => {
        event.preventDefault();
        submit();
      }}
    >
      <textarea
        ref={areaRef}
        className="composer__input"
        value={text}
        rows={1}
        placeholder={placeholder}
        onChange={(event) => setText(event.target.value)}
        onKeyDown={handleKeyDown}
        disabled={disabled}
        aria-label="消息输入框"
      />
      <button type="submit" className="btn btn--primary composer__send" disabled={!canSend}>
        {streaming ? '回复中…' : '发送'}
      </button>
    </form>
  );
}
