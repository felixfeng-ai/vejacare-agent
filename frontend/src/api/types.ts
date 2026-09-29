/**
 * 契约类型：严格对应 docs/API.md，字段名不得自行发明。
 *
 * 注意：SSE 事件由 JSON.parse 直接得到，运行期没有校验，
 * 类型只在编译期用于 switch 收窄，因此所有事件都带字面量 type 作为判别式。
 */

/* ------------------------------------------------------------------ *
 * 0. 通用信封
 * ------------------------------------------------------------------ */

export interface ApiErrorPayload {
  code: string;
  message: string;
}

export interface ApiSuccess<T> {
  success: true;
  data: T;
  error: null;
}

export interface ApiFailure {
  success: false;
  data: null;
  error: ApiErrorPayload;
}

export type Envelope<T> = ApiSuccess<T> | ApiFailure;

/* ------------------------------------------------------------------ *
 * 1. 基础实体
 * ------------------------------------------------------------------ */

export type JsonObject = Record<string, unknown>;

/** 意图枚举，与契约表格一一对应 */
export const INTENTS = [
  'logistics',
  'return_refund',
  'customs_duty',
  'size_fit',
  'payment',
  'coupon',
  'order_change',
  'human_agent',
  'chitchat',
] as const;

export type Intent = (typeof INTENTS)[number];

/** intent → 中文标签；历史消息里只有 intent 时用它兜底 */
export const INTENT_LABELS: Record<Intent, string> = {
  logistics: '物流跟踪',
  return_refund: '退换货',
  customs_duty: '关税政策',
  size_fit: '尺码选择',
  payment: '支付失败',
  coupon: '优惠券',
  order_change: '订单修改',
  human_agent: '转人工',
  chitchat: '闲聊',
};

/** 未知意图回退为原始字符串，避免界面出现空白 */
export function intentLabelOf(intent: string | null | undefined): string | null {
  if (!intent) return null;
  const table: Record<string, string> = INTENT_LABELS;
  return table[intent] ?? intent;
}

/** 图节点枚举 */
export type GraphNode =
  | 'intent_classifier'
  | 'kb_retriever'
  | 'tool_executor'
  | 'responder'
  | 'turn_evaluator'
  | 'escalation';

/** 槽位：契约示例为 order_no / tracking_no / sku，允许后端扩展其它键 */
export interface Slots {
  order_no?: string | null;
  tracking_no?: string | null;
  sku?: string | null;
  [key: string]: string | null | undefined;
}

/** 知识库命中片段 */
export interface KbDoc {
  title: string;
  source: string;
  score: number;
  snippet: string;
}

export type ToolStatus = 'running' | 'done' | 'error';

export interface ToolCall {
  name: string;
  status: ToolStatus;
  /** running 时只有 args */
  args?: JsonObject;
  /** done 时带 result 与 summary */
  result?: JsonObject;
  summary?: string;
}

/** 思考链上的一项：节点或工具卡片，保持后端下发顺序 */
export type TimelineItem =
  | { kind: 'node'; node: GraphNode; label: string }
  | { kind: 'tool'; tool: ToolCall };

/* ------------------------------------------------------------------ *
 * 2. 会话与消息（对应契约 1.2 / 1.3）
 * ------------------------------------------------------------------ */

export type Role = 'user' | 'assistant' | 'system' | 'human_agent';

export type SessionStatus = 'active' | 'escalated' | 'closed';

export interface Message {
  id: string;
  role: Role;
  content: string;
  intent: Intent | null;
  created_at: string;
  meta: JsonObject;
}

export interface SessionMessages {
  session_id: string;
  status: SessionStatus;
  messages: Message[];
}

export interface SessionSummary {
  session_id: string;
  title: string;
  status: SessionStatus;
  updated_at: string;
  message_count: number;
}

/* ------------------------------------------------------------------ *
 * 3. 满意度（对应契约 3.1 / 3.2）
 * ------------------------------------------------------------------ */

export interface FeedbackInput {
  session_id: string;
  message_id: string;
  rating: number;
  comment?: string;
}

export interface FeedbackResult {
  feedback_id: string;
}

export interface Metrics {
  total_sessions: number;
  rated_sessions: number;
  /**
   * 无评价时为 null —— 不是 0。
   *
   * 后端返回 null 而不是 0.0 是刻意的：「还没有人评分」和「平均 0 分」是两码事，
   * 用 0 表示前者会把看板上的平均分误导成极差。前端必须显式处理这个 null。
   * 这里原来写的是 `number`，是手写断言、不是从接口推导的，所以 tsc 查不出来。
   */
  avg_rating: number | null;
  rating_distribution: Record<string, number>;
  escalation_rate: number;
  auto_resolved_rate: number;
  top_intents: Array<{ intent: string; count: number }>;
}

/* ------------------------------------------------------------------ *
 * 4. SSE 事件（对应契约 1.1）—— 判别式联合
 * ------------------------------------------------------------------ */

export interface SessionEvent {
  type: 'session';
  session_id: string;
  created_at?: string;
}

export interface NodeEvent {
  type: 'node';
  node: GraphNode;
  label: string;
}

export interface IntentEvent {
  type: 'intent';
  intent: Intent;
  intent_label: string;
  slots: Slots;
}

export interface KbEvent {
  type: 'kb';
  docs: KbDoc[];
}

export interface ToolEvent {
  type: 'tool';
  name: string;
  status: ToolStatus;
  args?: JsonObject;
  result?: JsonObject;
  summary?: string;
}

export interface TokenEvent {
  type: 'token';
  text: string;
}

export interface EscalatedEvent {
  type: 'escalated';
  reason: string;
  reason_label: string;
  ticket_id: string;
}

export interface FeedbackRequestEvent {
  type: 'feedback_request';
  message_id: string;
}

export interface DoneEvent {
  type: 'done';
  message_id: string;
  intent: Intent | null;
  escalated: boolean;
}

export interface ErrorEvent {
  type: 'error';
  code: string;
  message: string;
}

/** 契约里出现的全部事件类型 */
export type ServerEvent =
  | SessionEvent
  | NodeEvent
  | IntentEvent
  | KbEvent
  | ToolEvent
  | TokenEvent
  | EscalatedEvent
  | FeedbackRequestEvent
  | DoneEvent
  | ErrorEvent;

const EVENT_TYPES: readonly string[] = [
  'session',
  'node',
  'intent',
  'kb',
  'tool',
  'token',
  'escalated',
  'feedback_request',
  'done',
  'error',
];

/** 运行期守卫：过滤 keep-alive、半截 JSON、后端新增的未知事件 */
export function isServerEvent(value: unknown): value is ServerEvent {
  if (typeof value !== 'object' || value === null) return false;
  const type: unknown = (value as { type?: unknown }).type;
  return typeof type === 'string' && EVENT_TYPES.includes(type);
}

/* ------------------------------------------------------------------ *
 * 5. 前端内部模型
 * ------------------------------------------------------------------ */

/** 界面上的一条消息；历史消息只填基础字段，实时消息带思考链等附加内容 */
export interface ChatMessage {
  id: string;
  role: Role;
  content: string;
  intent: Intent | null;
  /** 优先用后端下发的 intent_label */
  intentLabel?: string | null;
  createdAt: string;
  timeline?: TimelineItem[];
  kbDocs?: KbDoc[];
  slots?: Slots | null;
  /** 本轮已转人工（正文可能为空，由横幅/气泡说明） */
  escalated?: boolean;
  /** 出错文案，气泡内展示并给重试按钮 */
  error?: string;
  /** 正在打字机输出中 */
  streaming?: boolean;
}

/** 转人工信息 */
export interface EscalationInfo {
  reason: string;
  reasonLabel: string;
  ticketId: string | null;
}

/* ------------------------------------------------------------------ *
 * 6. 人工工单（对应契约 2.1 / 2.2 / 2.3 / 2.4）
 * ------------------------------------------------------------------ */

export type EscalationStatus = 'pending' | 'resolved';

/** 工单队列里的一条（契约 2.1 / 2.2 的公共部分） */
export interface EscalationSummary {
  id: string;
  session_id: string;
  reason: string;
  reason_label: string;
  status: EscalationStatus;
  /** 后端生成的一句话摘要，客服扫列表时先看它 */
  summary: string;
  human_reply: string | null;
  agent_name: string | null;
  created_at: string;
  resolved_at: string | null;
}

/**
 * 工单上下文：AI 当时据以作答的全部依据。
 *
 * 客服要判断「AI 为什么没解决」，靠的就是这里——用户说了什么、识别成什么意图、
 * 抽到哪些槽位、调了哪个工具、查到了什么。缺了它，客服只能让用户把问题重讲一遍。
 */
export interface EscalationContext {
  intent?: string | null;
  slots?: Slots | null;
  tool_results?: Array<{ name?: string; ok?: boolean; summary?: string }> | null;
  kb_docs?: KbDoc[] | null;
  messages?: Array<{ role: Role; content: string; created_at: string }>;
}

export interface EscalationDetail extends EscalationSummary {
  context: EscalationContext;
}

export interface EscalationReplyInput {
  reply: string;
  agent: string;
}

export interface EscalationReplyResult {
  escalation_id: string;
  session_id?: string;
  status: EscalationStatus;
  agent?: string;
  /** 人工回复后 AI 自动补的收尾话术，客服可据此确认对话已接上 */
  closing_message?: string;
}

/* ------------------------------------------------------------------ *
 * 7. 后台鉴权（对应契约第 6 节）
 * ------------------------------------------------------------------ */

/** 客服能办工单，管理者另可看经营看板 */
export type ConsoleRole = 'agent' | 'admin';

export const CONSOLE_ROLE_LABELS: Record<ConsoleRole, string> = {
  agent: '客服',
  admin: '管理员',
};

/** POST /api/console/login 的请求体 */
export interface ConsoleLoginInput {
  password: string;
  /** 客服署名，留空则用角色名 */
  name?: string;
}

/** GET /api/console/me 的 data；登录响应的身份部分 */
export interface ConsoleIdentity {
  role: ConsoleRole;
  name: string;
  /** unix 秒 */
  expires_at: number;
}

/** POST /api/console/login 的 data */
export interface ConsoleLoginResult extends ConsoleIdentity {
  token: string;
}

/**
 * 鉴权失败的错误码。前端据此分流：前两个要清令牌跳登录页，
 * 最后一个说明是服务端没配口令，跳登录页也没用，得在页面上讲清楚。
 */
export const CONSOLE_ERRORS = {
  unauthorized: 'CONSOLE_UNAUTHORIZED',
  tokenInvalid: 'CONSOLE_TOKEN_INVALID',
  forbidden: 'CONSOLE_FORBIDDEN',
  disabled: 'CONSOLE_DISABLED',
  badPassword: 'CONSOLE_BAD_PASSWORD',
  loginLocked: 'CONSOLE_LOGIN_LOCKED',
} as const;
