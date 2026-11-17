// JSON projections of the existing public Python DTOs. Parse once at the HTTP/SSE boundary.
export const statuses = [
  "ACCEPTED",
  "QUEUED",
  "RUNNING",
  "RETRY_WAIT",
  "WAITING_INPUT",
  "CANCELLING",
  "SUCCEEDED",
  "FAILED",
  "CANCELLED",
] as const;
export type ExecutionStatus = (typeof statuses)[number];
export const ingestionStages = [
  "REGISTERED",
  "PARSING",
  "NORMALIZING",
  "CHUNKING",
  "EMBEDDING",
  "INDEXING",
  "PUBLISHING",
] as const;
export type IngestionStage = (typeof ingestionStages)[number];
const progressStages = [
  ...ingestionStages.slice(1),
  "UNDERSTANDING",
  "APPLYING_CLARIFICATION",
  "SELECTING_TOOLS",
  "KEYWORD_SEARCH",
  "QUERY_EMBEDDING",
  "SEMANTIC_SEARCH",
  "RERANKING",
  "CHUNK_READ",
  "ASSESSING_EVIDENCE",
  "PREPARING_CLARIFICATION",
  "GENERATING_ANSWER",
  "DRAFTING_ANSWER",
  "REVIEWING_ANSWER",
  "PUBLISHING_ANSWER",
] as const;

export type Decoder<T> = (value: unknown) => T;
export function record(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value))
    throw new Error("接口响应不是对象");
  return value as Record<string, unknown>;
}
export function string(value: unknown): string {
  if (typeof value !== "string") throw new Error("接口字段不是字符串");
  return value;
}
function integer(value: unknown): number {
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value < 0)
    throw new Error("接口字段不是非负整数");
  return value;
}
export const nullable =
  <T>(decode: Decoder<T>): Decoder<T | null> =>
  (value) =>
    value === null ? null : decode(value);
export const array =
  <T>(decode: Decoder<T>): Decoder<T[]> =>
  (value) => {
    if (!Array.isArray(value)) throw new Error("接口字段不是列表");
    return value.map(decode);
  };
function enumeration<const T extends readonly string[]>(
  values: T,
): Decoder<T[number]> {
  return (value) => {
    const text = string(value);
    if (!values.includes(text)) throw new Error("接口枚举值无效");
    return text as T[number];
  };
}
export interface KnowledgeBaseItem {
  kb_id: string;
  name: string;
}
export interface KnowledgeBase extends KnowledgeBaseItem {
  revision: number;
  active_index_profile_id: string;
  created_at: string;
}
export interface DocumentItem {
  document_id: string;
  title: string;
  issuer: string;
}
export interface SourceReference {
  document_id: string;
  source_hash: string;
}
export interface SourceLocator extends SourceReference {
  block_ids: string[];
  page_start: number | null;
  page_end: number | null;
  section_path: string[];
  sheet_name: string | null;
  cell_range: string | null;
  paragraph_index: number | null;
}
export type VersionStatus = "CURRENT" | "HISTORICAL";
export interface PolicyDate {
  value: string;
  source: "USER" | "DOCUMENT";
  locators: SourceLocator[];
}
export interface SourceMetadataUpdate {
  new_job_id: string;
  version_status?: VersionStatus;
  effective_from?: string | null;
  effective_to?: string | null;
}
export interface PolicyMetadata {
  issuer: string;
  policy_code: string | null;
  version_status: VersionStatus;
  effective_from: PolicyDate | null;
  effective_to: PolicyDate | null;
  source_locators: SourceLocator[];
}
export interface JobStatusEvent {
  status: ExecutionStatus;
  next_retry_at: string | null;
  last_error_code: string | null;
  terminal_reason: string | null;
}
export interface IngestionJobView extends JobStatusEvent {
  job_id: string;
  stage: IngestionStage;
  retry_count: number;
  max_retries: number;
}
export interface DocumentSourceView {
  source_hash: string;
  uploaded_at: string;
  policy_metadata: PolicyMetadata;
  serving_generation_id: string | null;
  latest_job: IngestionJobView;
}
export interface DocumentView extends DocumentItem {
  kb_id: string;
  versions: DocumentSourceView[];
}
export interface IngestionAccepted extends SourceReference {
  job_id: string;
  status: "ACCEPTED";
  status_url: string;
}
export interface JobProgress {
  stage: string;
  item_id: string | null;
  tool_call_id: string | null;
  text: string;
}
export interface PublishedTextDelta {
  message_id: string;
  item_id: string | null;
  offset: number;
  text: string;
}
export interface PublishedMessageEvent {
  message_id: string;
}
export interface BusinessError {
  code: string;
  stage: string;
  retryable: boolean;
  user_message: string;
  detail_ref: string | null;
}

const knowledgeBaseFields = (v: Record<string, unknown>): KnowledgeBaseItem => {
  return { kb_id: string(v.kb_id), name: string(v.name) };
};
export const decodeKnowledgeBaseItem: Decoder<KnowledgeBaseItem> = (value) =>
  knowledgeBaseFields(record(value));
export const decodeKnowledgeBase: Decoder<KnowledgeBase> = (value) => {
  const v = record(value);
  return {
    ...knowledgeBaseFields(v),
    revision: integer(v.revision),
    active_index_profile_id: string(v.active_index_profile_id),
    created_at: string(v.created_at),
  };
};
const documentFields = (v: Record<string, unknown>): DocumentItem => {
  return {
    document_id: string(v.document_id),
    title: string(v.title),
    issuer: string(v.issuer),
  };
};
export const decodeDocumentItem: Decoder<DocumentItem> = (value) =>
  documentFields(record(value));
const sourceFields = (v: Record<string, unknown>): SourceReference => {
  return {
    document_id: string(v.document_id),
    source_hash: string(v.source_hash),
  };
};
const decodeLocator: Decoder<SourceLocator> = (value) => {
  const v = record(value);
  return {
    ...sourceFields(v),
    block_ids: array(string)(v.block_ids),
    page_start: nullable(integer)(v.page_start),
    page_end: nullable(integer)(v.page_end),
    section_path: array(string)(v.section_path),
    sheet_name: nullable(string)(v.sheet_name),
    cell_range: nullable(string)(v.cell_range),
    paragraph_index: nullable(integer)(v.paragraph_index),
  };
};
export const decodePolicyDate: Decoder<PolicyDate> = (value) => {
  const v = record(value);
  const text = string(v.value);
  if (!/^\d{4}-\d{2}(-\d{2})?$/.test(text)) throw new Error("Invalid policy date");
  const day = text.length === 7 ? `${text}-01` : text;
  const parsed = new Date(`${day}T00:00:00Z`);
  if (!Number.isFinite(parsed.getTime()) || parsed.toISOString().slice(0, 10) !== day) throw new Error("Invalid calendar date");
  return { value: text, source: enumeration(["USER", "DOCUMENT"] as const)(v.source), locators: array(decodeLocator)(v.locators) };
};
export const decodeMetadataUpdate: Decoder<SourceMetadataUpdate> = (value) => {
  const v = record(value);
  return {
    new_job_id: string(v.new_job_id),
    ...("version_status" in v ? { version_status: enumeration(["CURRENT", "HISTORICAL"] as const)(v.version_status) } : {}),
    ...("effective_from" in v ? { effective_from: nullable(string)(v.effective_from) } : {}),
    ...("effective_to" in v ? { effective_to: nullable(string)(v.effective_to) } : {}),
  };
};
const statusFields = (v: Record<string, unknown>): JobStatusEvent => {
  return {
    status: enumeration(statuses)(v.status),
    next_retry_at: nullable(string)(v.next_retry_at),
    last_error_code: nullable(string)(v.last_error_code),
    terminal_reason: nullable(string)(v.terminal_reason),
  };
};
export const decodeJobStatus: Decoder<JobStatusEvent> = (value) =>
  statusFields(record(value));
export const decodeIngestionJob: Decoder<IngestionJobView> = (value) => {
  const v = record(value);
  return {
    ...statusFields(v),
    job_id: string(v.job_id),
    stage: enumeration(ingestionStages)(v.stage),
    retry_count: integer(v.retry_count),
    max_retries: integer(v.max_retries),
  };
};
export const decodeDocument: Decoder<DocumentView> = (value) => {
  const v = record(value);
  return {
    ...documentFields(v),
    kb_id: string(v.kb_id),
    versions: array((source): DocumentSourceView => {
      const s = record(source);
      const p = record(s.policy_metadata);
      return {
        source_hash: string(s.source_hash),
        uploaded_at: string(s.uploaded_at),
        serving_generation_id: nullable(string)(s.serving_generation_id),
        latest_job: decodeIngestionJob(s.latest_job),
        policy_metadata: {
          issuer: string(p.issuer),
          policy_code: nullable(string)(p.policy_code),
          version_status: enumeration(["CURRENT", "HISTORICAL"] as const)(p.version_status),
          effective_from: nullable(decodePolicyDate)(p.effective_from),
          effective_to: nullable(decodePolicyDate)(p.effective_to),
          source_locators: array(decodeLocator)(p.source_locators),
        },
      };
    })(v.versions),
  };
};
export const decodeAccepted: Decoder<IngestionAccepted> = (value) => {
  const v = record(value);
  return {
    ...sourceFields(v),
    job_id: string(v.job_id),
    status: enumeration(["ACCEPTED"])(v.status),
    status_url: string(v.status_url),
  };
};
export const decodeProgress: Decoder<JobProgress> = (value) => {
  const v = record(value);
  return {
    stage: enumeration(progressStages)(v.stage),
    item_id: v.item_id === undefined ? null : nullable(string)(v.item_id),
    tool_call_id:
      v.tool_call_id === undefined ? null : nullable(string)(v.tool_call_id),
    text: string(v.text),
  };
};
export const decodeTextDelta: Decoder<PublishedTextDelta> = (value) => {
  const v = record(value);
  return {
    message_id: string(v.message_id),
    item_id: nullable(string)(v.item_id),
    offset: integer(v.offset),
    text: string(v.text),
  };
};
export const decodeMessage: Decoder<PublishedMessageEvent> = (value) => {
  const v = record(value);
  return { message_id: string(v.message_id) };
};
export const decodeBusinessError: Decoder<BusinessError> = (value) => {
  const v = record(value);
  if (typeof v.retryable !== "boolean")
    throw new Error("错误响应 retryable 无效");
  return {
    code: string(v.code),
    stage: string(v.stage),
    retryable: v.retryable,
    user_message: string(v.user_message),
    detail_ref: nullable(string)(v.detail_ref),
  };
};
export const isTerminal = (status: ExecutionStatus) =>
  status === "SUCCEEDED" || status === "FAILED" || status === "CANCELLED";

export interface QAJobView extends JobStatusEvent {
  job_id: string;
  retry_count: number;
  max_retries: number;
}
export interface QuestionCreate {
  message_id: string;
  kb_id: string;
  question: string;
}
export interface UserFact {
  fact_id: string;
  meaning: string;
  kind: "text" | "number" | "date" | "boolean";
  unit: string | null;
  source_message_id: string;
}
export interface SupportedClaim {
  claim_id: string;
  text: string;
  fact_ids: string[];
  evidence_chunk_ids: string[];
}
export interface EvidenceGap {
  kind: "USER_FACT" | "POLICY_MISSING" | "CONFLICT";
  description: string;
  evidence_chunk_ids: string[];
}
export interface Citation {
  number: number;
  chunk_id: string;
  source_text: string;
  locators: SourceLocator[];
}
export interface AnswerItem {
  item_id: string;
  question: string;
  coverage: "COMPLETE" | "PARTIAL" | "UNSUPPORTED";
  text: string;
  claims: SupportedClaim[];
  gaps: EvidenceGap[];
  citation_numbers: number[];
}
export interface AnswerResult {
  kind: "answer";
  items: AnswerItem[];
  facts: UserFact[];
  citations: Citation[];
}
export type PublicMessagePayload =
  | { kind: "question"; question: string; job_id: string }
  | AnswerResult
  | {
      kind: "clarification_request";
      interrupt_id: string;
      request: { item_ids: string[]; question: string };
    }
  | { kind: "clarification_reply"; text: string };
export interface PublicQAMessage {
  message_id: string;
  conversation_id: string;
  reply_to_message_id: string | null;
  payload: PublicMessagePayload;
  created_at: string;
}
export interface ConversationHistoryPage {
  conversation_id: string;
  active_job_id: string | null;
  messages: PublicQAMessage[];
  next_before: string | null;
}
export interface ClarificationReplyCreate {
  message_id: string;
  text: string;
}
export const decodeQAJob: Decoder<QAJobView> = (value) => {
  const v = record(value);
  return {
    ...statusFields(v),
    job_id: string(v.job_id),
    retry_count: integer(v.retry_count),
    max_retries: integer(v.max_retries),
  };
};
export const decodeQAAccepted = (value: unknown) => ({
  job_id: string(record(value).job_id),
});
export const decodeAnswer: Decoder<AnswerResult> = (value) => {
  const v = record(value);
  return {
    kind: enumeration(["answer"])(v.kind),
    items: array((value): AnswerItem => {
      const item = record(value);
      return {
        item_id: string(item.item_id),
        question: string(item.question),
        coverage: enumeration(["COMPLETE", "PARTIAL", "UNSUPPORTED"])(
          item.coverage,
        ),
        text: string(item.text),
        claims: array((value): SupportedClaim => {
          const claim = record(value);
          return {
            claim_id: string(claim.claim_id),
            text: string(claim.text),
            fact_ids: array(string)(claim.fact_ids),
            evidence_chunk_ids: array(string)(claim.evidence_chunk_ids),
          };
        })(item.claims),
        gaps: array((value): EvidenceGap => {
          const gap = record(value);
          return {
            kind: enumeration(["USER_FACT", "POLICY_MISSING", "CONFLICT"])(
              gap.kind,
            ),
            description: string(gap.description),
            evidence_chunk_ids: array(string)(gap.evidence_chunk_ids),
          };
        })(item.gaps),
        citation_numbers: array(integer)(item.citation_numbers),
      };
    })(v.items),
    facts: array((value): UserFact => {
      const fact = record(value);
      return {
        fact_id: string(fact.fact_id),
        meaning: string(fact.meaning),
        kind: enumeration(["text", "number", "date", "boolean"])(fact.kind),
        unit: nullable(string)(fact.unit),
        source_message_id: string(fact.source_message_id),
      };
    })(v.facts),
    citations: array((value): Citation => {
      const citation = record(value);
      return {
        number: integer(citation.number),
        chunk_id: string(citation.chunk_id),
        source_text: string(citation.source_text),
        locators: array(decodeLocator)(citation.locators),
      };
    })(v.citations),
  };
};
export const decodePublicMessage: Decoder<PublicQAMessage> = (value) => {
  const v = record(value);
  const p = record(v.payload);
  let payload: PublicMessagePayload;
  switch (p.kind) {
    case "question":
      payload = {
        kind: "question",
        question: string(p.question),
        job_id: string(p.job_id),
      };
      break;
    case "answer":
      payload = decodeAnswer(p);
      break;
    case "clarification_request": {
      const r = record(p.request);
      payload = {
        kind: "clarification_request",
        interrupt_id: string(p.interrupt_id),
        request: {
          item_ids: array(string)(r.item_ids),
          question: string(r.question),
        },
      };
      break;
    }
    case "clarification_reply":
      payload = { kind: "clarification_reply", text: string(p.text) };
      break;
    default:
      throw new Error("公开消息种类无效");
  }
  return {
    message_id: string(v.message_id),
    conversation_id: string(v.conversation_id),
    reply_to_message_id: nullable(string)(v.reply_to_message_id),
    created_at: string(v.created_at),
    payload,
  };
};
export const decodeHistory: Decoder<ConversationHistoryPage> = (value) => {
  const v = record(value);
  return {
    conversation_id: string(v.conversation_id),
    active_job_id: nullable(string)(v.active_job_id),
    messages: array(decodePublicMessage)(v.messages),
    next_before: nullable(string)(v.next_before),
  };
};

export const wikiTopics = [
  "business_context",
  "expense_category_and_scope",
  "process",
  "role",
  "relationship",
  "open_question",
] as const;
export type WikiTopic = (typeof wikiTopics)[number];
export interface WikiChunkRef {
  generation_id: string;
  chunk_id: string;
}
export interface WikiStatement {
  kind: "summary" | "relation" | "open_question";
  topic: WikiTopic;
  evidence_level: "direct" | "derived" | "inferred";
  text: string;
  chunk_refs: WikiChunkRef[];
  related_page_ids: string[];
}
export interface WikiPage {
  page_id: string;
  kind: "overview" | "document" | "synthesis";
  title: string;
  statements: WikiStatement[];
  child_page_ids: string[];
}
export interface WikiVersion {
  job_id: string;
  snapshot_revision: number;
  pages: Record<string, WikiPage>;
}
export interface WikiJobSummary {
  job_id: string;
  status: ExecutionStatus;
  operation: "build" | "edit";
}
export interface WikiWorkspace {
  kb_id: string;
  current_revision: number;
  published_version: WikiVersion | null;
  active_job: WikiJobSummary | null;
}
export interface WikiJob extends WikiJobSummary {
  kb_id: string;
  next_retry_at: string | null;
  last_error_code: string | null;
  terminal_reason: string | null;
  result_version_job_id: string | null;
  is_current: boolean;
}
export interface WikiChunkOption extends WikiChunkRef {
  ordinal: number;
  body_text: string;
  locators: SourceLocator[];
}
export interface WikiSourcePageEdit {
  page_id: string;
  statements: WikiStatement[];
}
export interface WikiSceneEdit {
  scene_ref: string;
  page_id: string | null;
  title: string;
  source_page_ids: string[];
  statements: WikiStatement[];
}
export interface WikiEditCreate {
  new_job_id: string;
  base_job_id: string;
  source_pages: WikiSourcePageEdit[];
  scenes: WikiSceneEdit[];
  overview: {
    business_context: { text: string; related_scene_refs: string[] };
    scenarios: { scene_ref: string; text: string }[];
  };
}
const decodeWikiChunkRef = (value: unknown): WikiChunkRef => {
  const v = record(value);
  return {
    generation_id: string(v.generation_id),
    chunk_id: string(v.chunk_id),
  };
};
const decodeWikiStatement = (value: unknown): WikiStatement => {
  const v = record(value);
  return {
    kind: enumeration(["summary", "relation", "open_question"] as const)(
      v.kind,
    ),
    topic: enumeration(wikiTopics)(v.topic),
    evidence_level: enumeration(["direct", "derived", "inferred"] as const)(
      v.evidence_level,
    ),
    text: string(v.text),
    chunk_refs: array(decodeWikiChunkRef)(v.chunk_refs),
    related_page_ids: array(string)(v.related_page_ids),
  };
};
const decodeWikiPage = (value: unknown): WikiPage => {
  const v = record(value);
  return {
    page_id: string(v.page_id),
    kind: enumeration(["overview", "document", "synthesis"] as const)(v.kind),
    title: string(v.title),
    statements: array(decodeWikiStatement)(v.statements),
    child_page_ids: array(string)(v.child_page_ids),
  };
};
const decodeWikiVersion = (value: unknown): WikiVersion => {
  const v = record(value);
  return {
    job_id: string(v.job_id),
    snapshot_revision: integer(v.snapshot_revision),
    pages: Object.fromEntries(
      Object.entries(record(v.pages)).map(([key, page]) => [
        key,
        decodeWikiPage(page),
      ]),
    ),
  };
};
const decodeWikiJobSummary = (value: unknown): WikiJobSummary => {
  const v = record(value);
  return {
    job_id: string(v.job_id),
    status: enumeration(statuses)(v.status),
    operation: enumeration(["build", "edit"] as const)(v.operation),
  };
};
export const decodeWikiWorkspace = (value: unknown): WikiWorkspace => {
  const v = record(value);
  return {
    kb_id: string(v.kb_id),
    current_revision: integer(v.current_revision),
    published_version: nullable(decodeWikiVersion)(v.published_version),
    active_job: nullable(decodeWikiJobSummary)(v.active_job),
  };
};
export const decodeWikiJob = (value: unknown): WikiJob => {
  const v = record(value);
  if (typeof v.is_current !== "boolean") throw new Error("接口字段不是布尔值");
  return {
    ...decodeWikiJobSummary(value),
    kb_id: string(v.kb_id),
    next_retry_at: nullable(string)(v.next_retry_at),
    last_error_code: nullable(string)(v.last_error_code),
    terminal_reason: nullable(string)(v.terminal_reason),
    result_version_job_id: nullable(string)(v.result_version_job_id),
    is_current: v.is_current,
  };
};
export const decodeWikiChunks = array((value: unknown): WikiChunkOption => {
  const v = record(value);
  return {
    ...decodeWikiChunkRef(v),
    ordinal: integer(v.ordinal),
    body_text: string(v.body_text),
    locators: array(decodeLocator)(v.locators),
  };
});
export function decodeWikiEdit(value: unknown): WikiEditCreate {
  const v = record(value),
    overview = record(v.overview),
    context = record(overview.business_context);
  return {
    new_job_id: string(v.new_job_id),
    base_job_id: string(v.base_job_id),
    source_pages: array((value) => {
      const s = record(value);
      return {
        page_id: string(s.page_id),
        statements: array(decodeWikiStatement)(s.statements),
      };
    })(v.source_pages),
    scenes: array((value) => {
      const s = record(value);
      return {
        scene_ref: string(s.scene_ref),
        page_id: nullable(string)(s.page_id),
        title: string(s.title),
        source_page_ids: array(string)(s.source_page_ids),
        statements: array(decodeWikiStatement)(s.statements),
      };
    })(v.scenes),
    overview: {
      business_context: {
        text: string(context.text),
        related_scene_refs: array(string)(context.related_scene_refs),
      },
      scenarios: array((value) => {
        const s = record(value);
        return { scene_ref: string(s.scene_ref), text: string(s.text) };
      })(overview.scenarios),
    },
  };
}
