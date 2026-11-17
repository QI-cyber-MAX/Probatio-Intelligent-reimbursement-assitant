import type {
  AnswerResult,
  Citation,
  PublicQAMessage,
  QAJobView,
} from "../../api/contracts";
import type { TextBuffer } from "./message-state";
import type { ActionEntry, SubmittedQuestion } from "./useConversation";
import type { ConnectionState } from "../../api/job-events";
import { AnswerItemCard } from "./AnswerItemCard";
import { JobActivity } from "./JobActivity";
import { StatusBadge } from "../../ui/StatusBadge";
import { ClarificationCard } from "./ClarificationCard";
import type { PendingReply } from "../../storage/pending-submissions";
export function MessageList({
  messages,
  submitted,
  answers,
  jobs,
  actions,
  buffers,
  activeId,
  connection,
  cancelling,
  onCancel,
  onCitation,
  selected,
  prompt,
  pendingReply,
  acceptedReply,
  replySending,
  replyFieldError,
  onReply,
}: {
  messages: PublicQAMessage[];
  submitted: SubmittedQuestion | null;
  answers: Record<string, AnswerResult>;
  jobs: Record<string, QAJobView>;
  actions: Record<string, ActionEntry[]>;
  buffers: TextBuffer[];
  activeId: string | null;
  connection: { state: ConnectionState; message?: string };
  cancelling: boolean;
  onCancel(id: string): void;
  onCitation(jobId: string, messageId: string | null, citation: Citation): void;
  selected: { jobId: string; number: number } | null;
  prompt: PublicQAMessage | null;
  pendingReply: PendingReply | null;
  acceptedReply: PendingReply | null;
  replySending: boolean;
  replyFieldError: string | null;
  onReply(text: string): void;
}) {
  const roots = messages.flatMap((m) =>
    m.payload.kind === "question"
      ? [
          {
            message_id: m.message_id,
            question: m.payload.question,
            job_id: m.payload.job_id,
          },
        ]
      : [],
  );
  if (
    submitted &&
    !roots.some((root) => root.message_id === submitted.message_id)
  )
    roots.push(submitted);
  return (
    <div className="message-list">
      {roots.map((root) => {
        const related = messages.filter(
          (m) => m.reply_to_message_id === root.message_id,
        );
        const answerMessage = related.find((m) => m.payload.kind === "answer");
        const answer =
          answerMessage?.payload.kind === "answer"
            ? answerMessage.payload
            : answers[root.job_id];
        const job = jobs[root.job_id];
        const citationsByChunk = new Map(answer?.citations.map((citation) => [citation.chunk_id, citation]));
        return (
          <article
            className="conversation-turn"
            key={root.message_id}
            data-message-id={root.message_id}
          >
            <div className="user-question">
              <span className="sr-only">问题：</span>
              <p className="message-text">{root.question}</p>
            </div>
            {job ? (
              <JobActivity
                job={job}
                actions={actions[root.job_id] ?? []}
                connection={activeId === root.job_id ? connection : undefined}
                cancelling={cancelling}
                onCancel={() => onCancel(root.job_id)}
                clarificationVisible={prompt?.reply_to_message_id === root.message_id}
              />
            ) : answer ? <StatusBadge tone="success">已完成</StatusBadge> : null}
            {related
              .filter((m) => m.payload.kind === "clarification_request")
              .map((message) => (
                <ClarificationCard key={message.message_id} prompt={message}
                  replies={messages.filter((m) => m.reply_to_message_id === message.message_id && m.payload.kind === "clarification_reply")}
                  current={prompt?.message_id === message.message_id && job?.status === "WAITING_INPUT"}
                  pending={pendingReply?.prompt_message_id === message.message_id ? pendingReply : null}
                  accepted={acceptedReply?.prompt_message_id === message.message_id ? acceptedReply : null}
                  sending={replySending} fieldError={prompt?.message_id === message.message_id ? replyFieldError : null}
                  onReply={onReply} cancelling={cancelling || job?.status === "CANCELLING"}
                  onCancel={() => onCancel(root.job_id)} />
              ))}
            {answer ? (
              <div className="formal-answer" data-job-id={root.job_id}>
                {answer.items.map((item, index) => (
                  <AnswerItemCard
                    key={item.item_id}
                    item={item}
                    index={index}
                    citationsByChunk={citationsByChunk}
                    selectedNumber={
                      selected?.jobId === root.job_id
                        ? selected.number
                        : undefined
                    }
                    onCitation={(number) => {
                      const citation = answer.citations.find(
                        (c) => c.number === number,
                      );
                      if (citation)
                        onCitation(
                          root.job_id,
                          answerMessage?.message_id ?? null,
                          citation,
                        );
                    }}
                  />
                ))}
              </div>
            ) : activeId === root.job_id && buffers.length > 0 ? (
              <div className="published-text">
                <p className="help">已发布文字 · 正在读取完整结果与引用</p>
                {buffers
                  .filter((buffer) => buffer.jobId === root.job_id)
                  .map((buffer) => (
                    <p
                      className="message-text"
                      key={`${buffer.message_id}:${buffer.item_id}`}
                    >
                      {buffer.text}
                    </p>
                  ))}
              </div>
            ) : job?.status === "SUCCEEDED" ? (
              <p className="help">正在读取正式答案…</p>
            ) : null}
          </article>
        );
      })}
      {activeId &&
        !roots.some((root) => root.job_id === activeId) &&
        jobs[activeId] && (
          <JobActivity
            job={jobs[activeId]}
            actions={actions[activeId] ?? []}
            connection={connection}
            cancelling={cancelling}
            onCancel={() => onCancel(activeId)}
          />
        )}
    </div>
  );
}
