/** 应用骨架：左侧会话栏 + 右侧聊天区 + 看板抽屉 */

import { useCallback, useState } from 'react';

import Composer from './components/Composer';
import EscalationBanner from './components/EscalationBanner';
import FeedbackCard from './components/FeedbackCard';
import MessageList from './components/MessageList';
import MetricsPanel from './components/MetricsPanel';
import Sidebar from './components/Sidebar';
import { useChat } from './hooks/useChat';
import { useSessions } from './hooks/useSessions';

export default function App() {
  const {
    sessionId,
    status,
    messages,
    liveMessage,
    streaming,
    loadingHistory,
    notice,
    escalation,
    escalationActive,
    feedback,
    inputLocked,
    send,
    retry,
    submitFeedback,
    dismissFeedback,
    dismissNotice,
    newSession,
    selectSession,
  } = useChat();

  const { sessions, loading, error, removingId, refresh, remove } = useSessions();

  const [drawerOpen, setDrawerOpen] = useState(false);
  const [metricsOpen, setMetricsOpen] = useState(false);

  const handleSend = useCallback(
    (text: string) => {
      // 一轮结束后刷新左侧列表（标题/条数/状态由后端更新）
      void send(text).then(() => {
        void refresh();
      });
    },
    [refresh, send],
  );

  const handleRetry = useCallback(() => {
    void retry().then(() => {
      void refresh();
    });
  }, [refresh, retry]);

  const handleNew = useCallback(() => {
    newSession();
    setDrawerOpen(false);
  }, [newSession]);

  return (
    <div className="app">
      <Sidebar
        sessions={sessions}
        currentId={sessionId}
        loading={loading}
        error={error}
        removingId={removingId}
        open={drawerOpen}
        onClose={() => setDrawerOpen(false)}
        onSelect={selectSession}
        onDelete={(id) => {
          void remove(id);
        }}
        onNew={handleNew}
        onRefresh={() => {
          void refresh();
        }}
        onOpenMetrics={() => setMetricsOpen(true)}
      />

      <main className="chat">
        <header className="chat__head">
          <button
            type="button"
            className="chat__menu"
            onClick={() => setDrawerOpen(true)}
            aria-label="打开历史会话"
          >
            ☰
          </button>
          <div className="chat__title">
            <h1 className="chat__name">VeyaCare 客服助手</h1>
            <span className="chat__sub">
              {escalationActive ? '已转接人工客服' : status === 'closed' ? '会话已结束' : '在线 · 支持物流/退换货/关税/尺码'}
            </span>
          </div>
          <button type="button" className="btn btn--ghost btn--sm chat__metrics" onClick={() => setMetricsOpen(true)}>
            数据看板
          </button>
        </header>

        {notice !== null && (
          <div className="notice" role="alert">
            <span>{notice}</span>
            <button type="button" className="notice__close" onClick={dismissNotice} aria-label="关闭提示">
              ×
            </button>
          </div>
        )}

        <MessageList
          messages={messages}
          liveMessage={liveMessage}
          loading={loadingHistory}
          onRetry={handleRetry}
          onSuggestion={handleSend}
        />

        <div className="chat__foot">
          {escalationActive && escalation !== null && <EscalationBanner info={escalation} />}

          {feedback !== null && (
            <FeedbackCard
              feedback={feedback}
              onSubmit={(rating, comment) => {
                void submitFeedback(rating, comment);
              }}
              onDismiss={dismissFeedback}
            />
          )}

          <Composer
            disabled={inputLocked}
            disabledHint="已转接人工客服，人工回复后即可继续对话"
            streaming={streaming}
            onSend={handleSend}
          />
        </div>
      </main>

      {metricsOpen && <MetricsPanel onClose={() => setMetricsOpen(false)} />}
    </div>
  );
}
