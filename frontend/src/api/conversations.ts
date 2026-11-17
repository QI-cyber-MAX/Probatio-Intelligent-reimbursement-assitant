import {
  decodeAnswer,
  decodeHistory,
  decodeQAAccepted,
  decodeQAJob,
  decodePublicMessage,
  record,
  string,
  type QuestionCreate,
  type ClarificationReplyCreate,
} from "./contracts";
import { request } from "./http";

const part = encodeURIComponent;
export const createConversation = (signal?: AbortSignal) =>
  request(
    "/conversations",
    (value) => ({
      conversation_id: string(record(value).conversation_id),
    }),
    { method: "POST", signal },
  );
export const askQuestion = (
  conversationId: string,
  question: QuestionCreate,
  signal?: AbortSignal,
) =>
  request(
    `/conversations/${part(conversationId)}/questions`,
    decodeQAAccepted,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(question),
      signal,
    },
  );
export const getQAJob = (id: string, signal?: AbortSignal) =>
  request(`/qa-runs/${part(id)}`, decodeQAJob, { signal });
export const getClarification = (id: string, signal?: AbortSignal) =>
  request(`/qa-runs/${part(id)}/clarification`, decodePublicMessage, { signal });
export const replyToClarification = (
  jobId: string,
  promptId: string,
  reply: ClarificationReplyCreate,
  signal?: AbortSignal,
) =>
  request(
    `/qa-runs/${part(jobId)}/clarifications/${part(promptId)}/replies`,
    decodeQAAccepted,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(reply),
      signal,
    },
  );
export const cancelQAJob = (id: string, signal?: AbortSignal) =>
  request(`/qa-runs/${part(id)}/cancel`, decodeQAJob, {
    method: "POST",
    signal,
  });
export const getAnswer = (id: string, signal?: AbortSignal) =>
  request(
    `/qa-runs/${part(id)}/answer`,
    (value, response) => {
      if (response.status === 202) {
        decodeQAAccepted(value);
        return null;
      }
      return decodeAnswer(value);
    },
    { signal },
  );
export const getHistory = (id: string, before?: string, signal?: AbortSignal) =>
  request(
    `/conversations/${part(id)}/messages${before ? `?before=${part(before)}` : ""}`,
    decodeHistory,
    { signal },
  );
