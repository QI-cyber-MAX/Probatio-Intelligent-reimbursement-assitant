import type {
  AnswerResult,
  PublicQAMessage,
  PublishedTextDelta,
} from "../../api/contracts";

export interface TextBuffer {
  jobId: string;
  message_id: string;
  item_id: string | null;
  text: string;
}
export interface MessageState {
  messages: PublicQAMessage[];
  answers: Record<string, AnswerResult>;
  buffers: TextBuffer[];
}
export const initialMessages: MessageState = {
  messages: [],
  answers: {},
  buffers: [],
};
export type MessageAction =
  | { type: "history"; messages: PublicQAMessage[] }
  | { type: "answer"; jobId: string; answer: AnswerResult }
  | { type: "delta"; jobId: string; delta: PublishedTextDelta }
  | { type: "reset-deltas" };

export function messageReducer(
  state: MessageState,
  action: MessageAction,
): MessageState {
  if (action.type === "reset-deltas") return { ...state, buffers: [] };
  if (action.type === "answer")
    return {
      ...state,
      answers: { ...state.answers, [action.jobId]: action.answer },
      buffers: state.buffers.filter((buffer) => buffer.jobId !== action.jobId),
    };
  if (action.type === "history") {
    const merged = new Map(
      state.messages.map((message) => [message.message_id, message]),
    );
    for (const message of action.messages)
      merged.set(message.message_id, message);
    const messages = [...merged.values()].sort(
      (a, b) =>
        a.created_at.localeCompare(b.created_at) ||
        a.message_id.localeCompare(b.message_id),
    );
    return {
      ...state,
      messages,
      buffers: state.buffers.filter((buffer) => !merged.has(buffer.message_id)),
    };
  }
  const delta = action.delta;
  if (state.answers[action.jobId]) return state;
  if (state.messages.some((m) => m.message_id === delta.message_id))
    return state;
  const index = state.buffers.findIndex(
    (b) => b.message_id === delta.message_id && b.item_id === delta.item_id,
  );
  const previous = index < 0 ? "" : state.buffers[index]!.text;
  const length = Array.from(previous).length;
  // Missing earlier text is recovered through the complete business message.
  if (delta.offset > length) return state;
  const addition = Array.from(delta.text)
    .slice(length - delta.offset)
    .join("");
  if (!addition) return state;
  const buffer = {
    jobId: action.jobId,
    message_id: delta.message_id,
    item_id: delta.item_id,
    text: previous + addition,
  };
  const buffers = [...state.buffers];
  if (index < 0) buffers.push(buffer);
  else buffers[index] = buffer;
  return { ...state, buffers };
}
