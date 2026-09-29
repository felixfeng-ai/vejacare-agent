/**
 * 客服工单台（/desk）。
 *
 * 为什么需要它：转人工这条链路后端一直是完整的——工单落库、队列、详情（含 AI 当时的
 * 完整上下文）、回复接口，连「回复后唤醒挂起的对话让 AI 收尾」都做好了。缺的只是
 * 一个给人用的界面，于是用户点了转人工之后：横幅写着「人工回复后即可继续对话」，
 * 而真人客服根本看不到这张工单，对话就永远停在那儿。
 *
 * 客服需要看到什么：光有一条工单标题不够。他得知道 AI 为什么没解决——用户原话、
 * 识别成什么意图、抽到哪些槽位、调了哪个工具、查回了什么。这些后端都存着（context），
 * 这里不做加工，原样铺开，客服才能接着往下办，而不是让用户把问题重讲一遍。
 *
 * 这里原来是用户端页面里的一个抽屉。抽屉是为了塞进别人家页面才有的形状——
 * 结果就是客服的工作台出现在用户眼前。既然有了路由，就该是一整页。
 */

import { useCallback, useEffect, useRef, useState } from 'react';

import ConsoleHeader from '../components/ConsoleHeader';
import { useAuth } from '../auth/AuthContext';
import { api, userMessageOf } from '../api/client';
import type { EscalationDetail, EscalationSummary } from '../api/types';

function formatTime(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleString('zh-CN', { hour12: false });
}

/** 上下文里的工具调用，字段名沿用后端 state 里的那份 */
function toolLine(tool: { name?: string; ok?: boolean; summary?: string }): string {
  const name = tool.name ?? '工具';
  if (tool.summary) return `${name}：${tool.summary}`;
  return `${name}：${tool.ok === false ? '查询失败' : '已完成'}`;
}

export default function DeskPage() {
  const { name } = useAuth();

  const [list, setList] = useState<EscalationSummary[]>([]);
  const [detail, setDetail] = useState<EscalationDetail | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [agent, setAgent] = useState(name);
  const [reply, setReply] = useState('');
  const [loading, setLoading] = useState(true);
  const [detailLoading, setDetailLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState<string | null>(null);

  // 署名默认取登录时填的那个（没填就是角色名），不再另存一份到 localStorage
  useEffect(() => {
    if (name !== '') setAgent(name);
  }, [name]);

  const loadList = useCallback(async (): Promise<void> => {
    setLoading(true);
    try {
      const rows = await api.listEscalations();
      setList(rows);
      setError(null);
      // 选中的那张还在就留着，不在了（例如刚被处理掉）就选第一张。
      //
      // 这里原来带一个 keepSelection 开关，进入页面和回复之后都传 false 把所有选中清掉
      // ——那是抽屉时代的写法（每次拉开都当新的一次）。放到整页上就成了：一进来右侧
      // 空着、客服得自己点一下才知道有内容。队列页的正确默认是「让人看见队首」。
      //
      // 更新函数必须是纯的（StrictMode 下会被调用两次），所以不在这里清 detail，
      // 交给下面那个跟随 selectedId 的 effect
      setSelectedId((current) => {
        if (current !== null && rows.some((row) => row.id === current)) return current;
        return rows[0]?.id ?? null;
      });
    } catch (err) {
      setError(userMessageOf(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadList();
  }, [loadList]);

  /**
   * 拉工单详情。队列和详情是两份数据，回复、结单之后两份都得刷新：
   * 只刷队列的话，客服刚发出去的那句话在自己的对话记录里看不见——
   * 界面显示「已回复」而线程里没有，客服只能怀疑是不是没发出去。
   */
  const detailSeqRef = useRef(0);

  const loadDetail = useCallback(async (id: string): Promise<void> => {
    detailSeqRef.current += 1;
    const seq = detailSeqRef.current;
    setDetailLoading(true);
    try {
      const data = await api.getEscalation(id);
      if (seq !== detailSeqRef.current) return; // 已经发起了更新的一次请求
      setDetail(data);
      setError(null);
    } catch (err) {
      if (seq !== detailSeqRef.current) return;
      setError(userMessageOf(err));
    } finally {
      if (seq === detailSeqRef.current) setDetailLoading(false);
    }
  }, []);

  // 换选中项就拉详情，上下文只有详情接口才返回
  useEffect(() => {
    if (selectedId === null) {
      setDetail(null);
      return;
    }
    void loadDetail(selectedId);
  }, [selectedId, loadDetail]);

  /** 发一句话给用户。**工单不结**——人工可以接着说，直到他自己点「结束会话」。 */
  const handleReply = useCallback(async (): Promise<void> => {
    if (detail === null || reply.trim().length === 0) return;
    setBusy(true);
    try {
      await api.replyEscalation(detail.id, {
        reply: reply.trim(),
        agent: agent.trim() === '' ? name : agent.trim(),
      });
      setReply('');
      setDone('已发出。会话仍在人工处理中，用户端还等着你结束会话。');
      setError(null);
      await Promise.all([loadList(), loadDetail(detail.id)]);
    } catch (err) {
      setError(userMessageOf(err));
    } finally {
      setBusy(false);
    }
  }, [agent, detail, loadDetail, loadList, name, reply]);

  /**
   * 结束会话：结单、用户端解锁、AI 补一句收尾。
   *
   * 收尾那句话由后端按模板拼，不是 AI 生成的——人工说完就说完了，模型没有信息可生成。
   */
  const handleClose = useCallback(async (): Promise<void> => {
    if (detail === null) return;
    setBusy(true);
    try {
      const result = await api.closeEscalation(detail.id);
      setDone(
        result.closing_message
          ? `已结束会话，用户端收到：「${result.closing_message}」`
          : '已结束会话，用户端可以继续对话了',
      );
      setError(null);
      await Promise.all([loadList(), loadDetail(detail.id)]);
    } catch (err) {
      setError(userMessageOf(err));
    } finally {
      setBusy(false);
    }
  }, [detail, loadDetail, loadList]);

  const pending = list.filter((row) => row.status === 'pending').length;

  return (
    <div className="page">
      <ConsoleHeader
        title="人工工单台"
        badge={pending > 0 ? `${pending} 张待处理` : undefined}
        onRefresh={() => void loadList()}
        refreshing={loading}
      />

      {error !== null && (
        <p className="desk__error" role="alert">
          {error}
        </p>
      )}
      {done !== null && <p className="desk__done">{done}</p>}

      {loading && <p className="page__hint">加载中…</p>}

      {!loading && list.length === 0 && (
        <p className="page__hint">
          暂无工单。用户在对话里说「转人工」时，这里会出现待处理工单。
        </p>
      )}

      {!loading && list.length > 0 && (
        <div className="desk">
          <ul className="desk__list">
            {list.map((row) => (
              <li key={row.id}>
                <button
                  type="button"
                  className={`desk__item${row.id === selectedId ? ' desk__item--on' : ''}`}
                  onClick={() => {
                    setSelectedId(row.id);
                    setDone(null);
                  }}
                >
                  <span className="desk__item-top">
                    <span className={`desk__tag desk__tag--${row.status}`}>
                      {row.status === 'pending' ? '待处理' : '已处理'}
                    </span>
                    {/* 用户在你处理期间又补了话。没有这个标，客服不主动刷新就以为对方在干等 */}
                    {row.status === 'pending' && row.unread_count > 0 && (
                      <span className="desk__unread">{row.unread_count} 条新消息</span>
                    )}
                    <span className="desk__time">{formatTime(row.created_at)}</span>
                  </span>
                  <span className="desk__item-title">{row.summary || row.reason_label}</span>
                  <span className="desk__item-sub">工单 {row.id}</span>
                </button>
              </li>
            ))}
          </ul>

          <div className="desk__detail">
            {detailLoading && <p className="page__hint">读取工单…</p>}
            {!detailLoading && detail === null && <p className="page__hint">左侧选一张工单</p>}

            {!detailLoading && detail !== null && (
              <>
                <dl className="desk__meta">
                  <div>
                    <dt>转人工原因</dt>
                    <dd>{detail.reason_label}</dd>
                  </div>
                  <div>
                    <dt>会话</dt>
                    <dd>{detail.session_id}</dd>
                  </div>
                  <div>
                    <dt>提交时间</dt>
                    <dd>{formatTime(detail.created_at)}</dd>
                  </div>
                  <div>
                    <dt>AI 识别意图</dt>
                    <dd>{detail.context.intent ?? '—'}</dd>
                  </div>
                </dl>

                {(detail.context.slots ?? null) !== null && (
                  <p className="desk__row">
                    <span className="desk__row-key">槽位</span>
                    <span className="desk__row-val">
                      {Object.entries(detail.context.slots ?? {})
                        .filter(([, value]) => value !== null && value !== undefined && value !== '')
                        .map(([key, value]) => `${key}=${String(value)}`)
                        .join('、') || '未抽到'}
                    </span>
                  </p>
                )}

                {(detail.context.tool_results ?? []).length > 0 && (
                  <p className="desk__row">
                    <span className="desk__row-key">工具</span>
                    <span className="desk__row-val">
                      {(detail.context.tool_results ?? []).map(toolLine).join('；')}
                    </span>
                  </p>
                )}

                <h3 className="desk__sub-title">对话记录</h3>
                {detail.status === 'pending' && detail.unread_count > 0 && (
                  <p className="desk__note">
                    用户在你接手后又说了 {detail.unread_count} 句，都在下面——
                    等待期间 AI 不答话，但话都替用户留着。
                  </p>
                )}
                <div className="desk__thread">
                  {(detail.context.messages ?? []).length === 0 && (
                    <p className="page__hint">没有取到对话记录</p>
                  )}
                  {(detail.context.messages ?? []).map((message, index) => (
                    <div
                      key={`${message.created_at}-${index}`}
                      className={`desk__msg desk__msg--${message.role}`}
                    >
                      <span className="desk__msg-role">
                        {message.role === 'user'
                          ? '用户'
                          : message.role === 'human_agent'
                            ? `人工客服${detail.agent_name ? `（${detail.agent_name}）` : ''}`
                            : 'AI'}
                      </span>
                      <span className="desk__msg-text">{message.content}</span>
                    </div>
                  ))}
                </div>

                {detail.status === 'pending' ? (
                  <div className="desk__reply">
                    <label className="desk__label" htmlFor="desk-agent">
                      客服署名
                    </label>
                    <input
                      id="desk-agent"
                      className="desk__input"
                      value={agent}
                      maxLength={64}
                      onChange={(event) => setAgent(event.target.value)}
                    />
                    <label className="desk__label" htmlFor="desk-reply">
                      回复内容
                    </label>
                    <textarea
                      id="desk-reply"
                      className="desk__textarea"
                      value={reply}
                      maxLength={4000}
                      rows={3}
                      placeholder="写清处理结论或下一步，以「人工客服」身份进入对话。可以分几次说，说完再点「结束会话」"
                      onChange={(event) => setReply(event.target.value)}
                    />
                    <p className="desk__note">
                      发送回复只是说一句话，会话仍在你手上（AI 不会插话）。
                      办完了点「结束会话」，用户端才会解锁。
                    </p>
                    <div className="desk__actions">
                      <button
                        type="button"
                        className="btn btn--primary btn--sm"
                        onClick={() => void handleReply()}
                        disabled={busy || reply.trim().length === 0}
                      >
                        {busy ? '提交中…' : '发送回复'}
                      </button>
                      <button
                        type="button"
                        className="btn btn--ghost btn--sm"
                        onClick={() => void handleClose()}
                        disabled={busy}
                      >
                        结束会话
                      </button>
                    </div>
                  </div>
                ) : (
                  <p className="desk__closed">
                    该工单已由 {detail.agent_name ?? '人工客服'} 处理
                    {detail.resolved_at !== null ? `（${formatTime(detail.resolved_at)}）` : ''}
                    {detail.human_reply !== null ? `：${detail.human_reply}` : ''}
                  </p>
                )}
              </>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
