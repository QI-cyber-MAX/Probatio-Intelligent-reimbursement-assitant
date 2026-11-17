import { useRef, useState, type FormEvent } from "react";
import type { PublicQAMessage } from "../../api/contracts";
import type { PendingReply } from "../../storage/pending-submissions";
import { StatusBadge } from "../../ui/StatusBadge";

export function ClarificationCard({ prompt, replies, current, pending, accepted, sending,
  fieldError, onReply, cancelling, onCancel }: {
  prompt: PublicQAMessage;
  replies: PublicQAMessage[];
  current: boolean;
  pending: PendingReply | null;
  accepted: PendingReply | null;
  sending: boolean;
  fieldError: string | null;
  onReply(text: string): void;
  cancelling: boolean;
  onCancel(): void;
}) {
  const [text, setText] = useState(pending?.text ?? accepted?.text ?? "");
  const input = useRef<HTMLTextAreaElement>(null);
  if (prompt.payload.kind !== "clarification_request") return null;
  const answered = replies.length > 0 || accepted !== null;
  const editable = current && !answered;
  const inputId = `reply-${prompt.message_id}`;
  function submit(event: FormEvent) {
    event.preventDefault();
    input.current?.focus();
    if (editable && !sending && !pending) onReply(text);
  }
  return (
    <section className={`clarification-card${editable ? " current" : ""}`} data-prompt-id={prompt.message_id}>
      <div className="clarification-heading">
        <h2>需要补充信息</h2>
        <StatusBadge tone={editable ? "warning" : "neutral"}>
          {answered ? "已回复" : pending ? "待核对" : editable ? "待回复" : "历史提示"}
        </StatusBadge>
      </div>
      <p className="message-text">{prompt.payload.request.question}</p>
      <p className="help">关联问题项：{prompt.payload.request.item_ids.join("、")}</p>
      {replies.map((reply) => reply.payload.kind === "clarification_reply" && (
        <p className="message-text clarification-reply" key={reply.message_id}>{reply.payload.text}</p>
      ))}
      {accepted && !replies.length && <p className="message-text clarification-reply">{accepted.text}</p>}
      {answered && <p className="help">已提交补充，继续处理。</p>}
      {(editable || pending) && !answered && (
        <form onSubmit={submit}>
          <label htmlFor={inputId}>补充信息</label>
          <textarea id={inputId} ref={input} rows={3} value={pending?.text ?? text}
            readOnly={sending || !!pending} aria-invalid={!!fieldError}
            aria-describedby={fieldError ? `${inputId}-error` : undefined}
            onChange={(event) => setText(event.target.value)} />
          {fieldError && <p className="field-error" id={`${inputId}-error`} role="alert">{fieldError}</p>}
          <div className="inline-actions">
            <button className="button primary" type="submit" disabled={sending || !!pending || !editable}>
              {sending ? "正在提交补充…" : "提交补充"}
            </button>
            {editable && <button className="text-button danger-text" type="button" disabled={cancelling}
              onClick={onCancel}>取消任务</button>}
          </div>
        </form>
      )}
    </section>
  );
}
