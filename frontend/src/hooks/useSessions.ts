/**
 * 历史会话列表（左侧栏）：拉取、刷新、删除。
 */

import { useCallback, useEffect, useState } from 'react';

import { api, userMessageOf } from '../api/client';
import type { SessionSummary } from '../api/types';

export interface UseSessionsResult {
  sessions: SessionSummary[];
  loading: boolean;
  error: string | null;
  /** 正在删除的会话 id，用于禁用按钮 */
  removingId: string | null;
  refresh: () => Promise<void>;
  remove: (sessionId: string) => Promise<void>;
}

export function useSessions(): UseSessionsResult {
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [removingId, setRemovingId] = useState<string | null>(null);

  const refresh = useCallback(async (): Promise<void> => {
    setLoading(true);
    try {
      const list = await api.listSessions();
      setSessions(list);
      setError(null);
    } catch (err) {
      setError(userMessageOf(err));
    } finally {
      setLoading(false);
    }
  }, []);

  const remove = useCallback(async (sessionId: string): Promise<void> => {
    setRemovingId(sessionId);
    try {
      await api.deleteSession(sessionId);
      // 不可变更新：过滤掉被删除项
      setSessions((previous) => previous.filter((item) => item.session_id !== sessionId));
      setError(null);
    } catch (err) {
      setError(userMessageOf(err));
    } finally {
      setRemovingId(null);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  return { sessions, loading, error, removingId, refresh, remove };
}
