import { useEffect, useLayoutEffect, useRef, useState } from "react";
import type { Citation } from "../../api/contracts";
import { ErrorNotice } from "../../ui/ErrorNotice";
import { MessageList } from "./MessageList";
import { QuestionComposer } from "./QuestionComposer";
import { useConversation } from "./useConversation";
import { qaStatusLabels } from "./job-labels";

export function ConversationView({
  conversationId,
  kb,
  onCreated,
  onBusyChange,
  onCitation,
  selected,
}: {
  conversationId: string | null;
  kb: string | null;
  onCreated(id: string): void;
  onBusyChange(value: boolean): void;
  onCitation(jobId: string, messageId: string | null, citation: Citation): void;
  selected: { jobId: string; number: number } | null;
}) {
  const chat = useConversation(conversationId, onCreated);
  const scroll = useRef<HTMLDivElement>(null);
  const nearBottom = useRef(true);
  const prepend = useRef(false);
  const readingAnchor = useRef<{ node: HTMLElement; top: number } | null>(null);
  const adjustedScroll = useRef<number | null>(null);
  const [newMessages, setNewMessages] = useState(false);
  useEffect(() => {
    onBusyChange(chat.busy || !!chat.pending || !!chat.pendingReply);
    return () => onBusyChange(false);
  }, [chat.busy, chat.pending, chat.pendingReply, onBusyChange]);
  useLayoutEffect(() => {
    const element = scroll.current;
    if (!element) return;
    if (prepend.current && chat.loadingOlder) return;
    const anchor = readingAnchor.current;
    if ((prepend.current || !nearBottom.current) && anchor?.node.isConnected) {
      element.scrollTop += anchor.node.getBoundingClientRect().top - anchor.top;
      adjustedScroll.current = element.scrollTop;
      if (!prepend.current) setNewMessages(true);
    } else if (nearBottom.current) element.scrollTop = element.scrollHeight;
    else setNewMessages(true);
    prepend.current = false;
  }, [
    chat.messages,
    chat.answers,
    chat.buffers,
    chat.actions,
    chat.loadingOlder,
    chat.prompt,
    chat.pendingReply,
    chat.jobs,
  ]);
  function toBottom() {
    if (scroll.current) scroll.current.scrollTop = scroll.current.scrollHeight;
    nearBottom.current = true;
    setNewMessages(false);
  }
  function rememberPosition() {
    const element = scroll.current;
    if (!element) return;
    const top = element.getBoundingClientRect().top;
    const node = [...element.querySelectorAll<HTMLElement>(".conversation-turn")]
      .find((turn) => turn.getBoundingClientRect().bottom > top);
    readingAnchor.current = node ? { node, top: node.getBoundingClientRect().top } : null;
  }
  return (
    <div className="conversation-view">
      <h1 id="page-title" className="sr-only" tabIndex={-1}>
        制度问答
      </h1>
      <div
        className="conversation-scroll"
        ref={scroll}
        onScroll={() => {
          const el = scroll.current!;
          // Layout correction must not select a newly prepended neighbour as the anchor.
          if (adjustedScroll.current !== null && Math.abs(el.scrollTop - adjustedScroll.current) < 1) {
            adjustedScroll.current = null;
            return;
          }
          adjustedScroll.current = null;
          nearBottom.current =
            el.scrollHeight - el.scrollTop - el.clientHeight < 80;
          if (nearBottom.current) setNewMessages(false);
          if (!prepend.current) rememberPosition();
        }}
      >
        {chat.historyError !== null && (
          <ErrorNotice error={chat.historyError} retry={chat.retry} />
        )}
        {chat.error !== null && (
          <ErrorNotice error={chat.error} retry={chat.retry} />
        )}
        {chat.replyError !== null && <ErrorNotice error={chat.replyError} retry={chat.retry} />}
        {chat.storageWarning && (
          <p className="help">
            浏览器存储不可用，刷新后可能无法恢复本次提交身份。
          </p>
        )}
        {chat.loading && !chat.messages.length && (
          <p className="muted">正在读取会话…</p>
        )}
        {chat.before && (
          <button
            className="text-button"
            disabled={chat.loadingOlder}
            onClick={() => {
              rememberPosition();
              prepend.current = true;
              void chat.loadOlder();
            }}
          >
            {chat.loadingOlder ? "正在读取…" : "加载较早消息"}
          </button>
        )}
        {!chat.loading &&
          chat.historyError === null &&
          !chat.messages.length &&
          !chat.submitted && (
            <div className="empty-state chat-empty">
              <h2>有什么制度问题？</h2>
              {!kb && <p className="help">选择知识库后开始提问。</p>}
            </div>
          )}
        <MessageList
          messages={chat.messages}
          submitted={chat.submitted}
          answers={chat.answers}
          jobs={chat.jobs}
          actions={chat.actions}
          buffers={chat.buffers}
          activeId={chat.activeId}
          connection={chat.connection}
          cancelling={chat.cancelling}
          onCancel={(id) => {
            void chat.cancel(id);
          }}
          onCitation={onCitation}
          selected={selected}
          prompt={chat.prompt}
          pendingReply={chat.pendingReply}
          acceptedReply={chat.acceptedReply}
          replySending={chat.replySending}
          replyFieldError={chat.replyFieldError}
          onReply={(text) => { void chat.reply(text); }}
        />
        {chat.pending && (
          <div className="pending-question">
            <p>问题受理结果未确认，已保留原消息身份和原文。</p>
            <button className="button secondary" disabled={chat.loading} onClick={chat.retry}>
              核对问题结果
            </button>
            <button
              className="button secondary"
              disabled={chat.busy || !chat.historyReady || chat.loading}
              onClick={() => {
                void chat.send(kb, true);
              }}
            >
              重传相同问题
            </button>
          </div>
        )}
        {chat.pendingReply && !chat.replySending && (
          <div className="pending-question">
            <p>补充受理结果未确认，已保留原回复身份和原文。</p>
            <p className="message-text">{chat.pendingReply.text}</p>
            <div className="inline-actions">
              <button className="button secondary" onClick={chat.retry} disabled={chat.loading}>核对补充结果</button>
              <button className="button secondary" disabled={!chat.historyReady || chat.loading}
                onClick={() => { void chat.reply(chat.pendingReply!.text, true); }}>重传相同补充</button>
            </div>
          </div>
        )}
      </div>
      <div className="conversation-footer">
        {newMessages && (
          <button className="button secondary new-messages" onClick={toBottom}>
            有新消息 ↓
          </button>
        )}
        <div className="sr-only" role="status" aria-live="polite">
          {chat.replySending ? "正在提交补充" : chat.sending
            ? "正在发送问题"
            : chat.activeId
              ? `当前任务：${chat.jobs[chat.activeId] ? qaStatusLabels[chat.jobs[chat.activeId]!.status] : "正在读取状态"}`
              : "可以提问"}
        </div>
        <QuestionComposer
          value={chat.draft}
          error={chat.questionError}
          onChange={chat.setDraft}
          sending={chat.sending}
          readOnly={!!chat.pending}
          disabled={chat.busy || !kb || !chat.historyReady || !!chat.pending || !!chat.pendingReply}
          onSend={() => {
            void chat.send(kb);
          }}
          hint={
            chat.pendingReply ? "先核对未确认的补充" : chat.pending
              ? "先核对未确认的问题"
              : chat.busy
                ? "当前任务结束后可发送新问题，草稿会保留"
                : !kb
                  ? "请先选择下一轮知识库"
                  : null
          }
        />
      </div>
    </div>
  );
}
