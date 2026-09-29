/**
 * 满意度看板（/admin）。
 *
 * 这是经营数据：整体会话量、自动解决率、转人工率、评分走势。它回答的是
 * 「这个客服机器人到底顶不顶用」，不是「张三那单退了没有」——后者是客服
 * 看单条工单就能办的事。所以这一页只给管理员，客服令牌进来会被后端 403。
 *
 * 权限判在一处：路由守卫（前端，拦在渲染前）与接口依赖（后端，真正的边界）。
 * 前端那层只是不让客服看到一个点了就报错的链接，拦不住任何有意的越权——
 * 看板数据从来不是靠"用户端没这个按钮"保护的。
 *
 * 原先它是用户端页面里的一个抽屉，就挂在聊天页页头上、谁都能点开。
 */

import { useCallback, useEffect, useState } from 'react';

import ConsoleHeader from '../components/ConsoleHeader';
import { api, userMessageOf } from '../api/client';
import { intentLabelOf } from '../api/types';
import type { Metrics } from '../api/types';

const MAX_STARS = 5;
const DIST_KEYS = ['1', '2', '3', '4', '5'] as const;

function formatPercent(value: number): string {
  return `${(value * 100).toFixed(1)}%`;
}

export default function AdminPage() {
  const [metrics, setMetrics] = useState<Metrics | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async (): Promise<void> => {
    setLoading(true);
    try {
      setMetrics(await api.getMetrics());
      setError(null);
    } catch (err) {
      setError(userMessageOf(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const maxIntentCount = metrics === null
    ? 0
    : metrics.top_intents.reduce((max, item) => Math.max(max, item.count), 0);
  const maxDist = metrics === null
    ? 0
    : DIST_KEYS.reduce((max, key) => Math.max(max, metrics.rating_distribution[key] ?? 0), 0);

  return (
    <div className="page">
      <ConsoleHeader title="满意度看板" onRefresh={() => void load()} refreshing={loading} />

      {error !== null && (
        <p className="desk__error" role="alert">
          {error}
        </p>
      )}
      {loading && <p className="page__hint">加载中…</p>}

      {!loading && metrics !== null && (
        <div className="metrics">
          <div className="metrics__grid">
            <div className="metric">
              <span className="metric__label">总会话</span>
              <span className="metric__value">{metrics.total_sessions}</span>
            </div>
            <div className="metric">
              <span className="metric__label">已评价</span>
              <span className="metric__value">{metrics.rated_sessions}</span>
            </div>
            <div className="metric">
              <span className="metric__label">平均分</span>
              {/* avg_rating 在无人评分时是 null（后端刻意区分「没评分」与「0 分」），
                  直接 .toFixed 会抛 TypeError 把整个看板打崩 */}
              <span className="metric__value">
                {metrics.avg_rating === null ? (
                  <small className="metric__unit">暂无评分</small>
                ) : (
                  <>
                    {metrics.avg_rating.toFixed(2)}
                    <small className="metric__unit">/{MAX_STARS}</small>
                  </>
                )}
              </span>
            </div>
            <div className="metric">
              <span className="metric__label">转人工率</span>
              <span className="metric__value">{formatPercent(metrics.escalation_rate)}</span>
            </div>
            <div className="metric">
              <span className="metric__label">自动解决率</span>
              <span className="metric__value">{formatPercent(metrics.auto_resolved_rate)}</span>
            </div>
          </div>

          <h3 className="metrics__subtitle">评分分布</h3>
          <ul className="bars">
            {DIST_KEYS.map((key) => {
              const count = metrics.rating_distribution[key] ?? 0;
              const width = maxDist === 0 ? 0 : Math.round((count / maxDist) * 100);
              return (
                <li key={key} className="bars__row">
                  <span className="bars__label">{key} 星</span>
                  <span className="bars__track">
                    <span className="bars__fill" style={{ width: `${width}%` }} />
                  </span>
                  <span className="bars__count">{count}</span>
                </li>
              );
            })}
          </ul>

          <h3 className="metrics__subtitle">高频意图</h3>
          {metrics.top_intents.length === 0 && <p className="page__hint">暂无数据</p>}
          <ul className="bars">
            {metrics.top_intents.map((item) => {
              const width = maxIntentCount === 0 ? 0 : Math.round((item.count / maxIntentCount) * 100);
              return (
                <li key={item.intent} className="bars__row">
                  <span className="bars__label">{intentLabelOf(item.intent)}</span>
                  <span className="bars__track">
                    <span className="bars__fill bars__fill--alt" style={{ width: `${width}%` }} />
                  </span>
                  <span className="bars__count">{item.count}</span>
                </li>
              );
            })}
          </ul>
        </div>
      )}
    </div>
  );
}
