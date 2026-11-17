import {
  useCallback,
  useEffect,
  useMemo,
  useReducer,
  useRef,
  useState,
} from "react";
import {
  decodeQAJob,
  isTerminal,
  type JobProgress,
  type QAJobView,
  type QuestionCreate,
  type PublicQAMessage,
} from "../../api/contracts";
import {
  askQuestion,
  cancelQAJob,
  createConversation,
  getAnswer,
  getHistory,
  getQAJob,
  getClarification,
  replyToClarification,
} from "../../api/conversations";
import { ApiError } from "../../api/http";
import type { ConnectionState, EventHandlers } from "../../api/job-events";
import { useJobEvents } from "../../api/useJobEvents";
import { createUuid } from "../../uuid";
import {
  clearPendingQuestion,
  readPendingQuestion,
  savePendingQuestion,
  type PendingQuestion,
  clearPendingReply,
  readPendingReply,
  savePendingReply,
  type PendingReply,
} from "../../storage/pending-submissions";
import { initialMessages, messageReducer } from "./message-state";

export interface SubmittedQuestion extends QuestionCreate {
  job_id: string;
}
export interface ActionEntry extends JobProgress {
  sequence: number;
}

// History pages may start at a child message. Read back to its real parent.
async function readPage(
  id: string,
  before: string | undefined,
  signal: AbortSignal,
) {
  const page = await getHistory(id, before, signal);
  const messages = [...page.messages];
  let cursor = page.next_before;
  while (cursor) {
    const known = new Set(messages.map((m) => m.message_id));
    const missing = new Set(
      messages.flatMap((m) =>
        m.reply_to_message_id && !known.has(m.reply_to_message_id)
          ? [m.reply_to_message_id]
          : [],
      ),
    );
    if (!missing.size) break;
    const older = await getHistory(id, cursor, signal);
    const firstParent = older.messages.findIndex((m) => missing.has(m.message_id));
    const start = Math.max(0, firstParent);
    messages.unshift(...older.messages.slice(start));
    cursor = start > 0 ? older.messages[start]!.message_id : older.next_before;
  }
  return { ...page, messages, next_before: cursor };
}

export function useConversation(
  initialId: string | null,
  onCreated: (id: string) => void,
) {
  const [id, setId] = useState(initialId);
  const [state, dispatch] = useReducer(messageReducer, initialMessages);
  const [jobs, setJobs] = useState<Record<string, QAJobView>>({});
  const [activeId, setActiveId] = useState<string | null>(null);
  const [submitted, setSubmitted] = useState<SubmittedQuestion | null>(null);
  const [restored] = useState(() => {
    let unavailable = false;
    const warn = () => {
      unavailable = true;
    };
    return {
      question: initialId ? readPendingQuestion(initialId, warn) : null,
      reply: initialId ? readPendingReply(initialId, warn) : null,
      unavailable,
    };
  });
  const [pending, setPending] = useState(restored.question);
  const pendingRef = useRef<PendingQuestion | null>(pending);
  const [draft, setDraft] = useState(pending?.question ?? "");
  const [sending, setSending] = useState(false);
  const sendingRef = useRef(false);
  const [cancelling, setCancelling] = useState(false);
  const [loading, setLoading] = useState(initialId !== null);
  const [loadingOlder, setLoadingOlder] = useState(false);
  const [historyReady, setHistoryReady] = useState(initialId === null);
  const [before, setBefore] = useState<string | null>(null);
  const firstHistory = useRef(true);
  const [error, setError] = useState<unknown>(null);
  const [questionError, setQuestionError] = useState<string | null>(null);
  const [historyError, setHistoryError] = useState<unknown>(null);
  const [storageWarning, setStorageWarning] = useState(restored.unavailable);
  const [pendingReply, setPendingReply] = useState(restored.reply);
  const pendingReplyRef = useRef<PendingReply | null>(restored.reply);
  const [prompt, setPrompt] = useState<{
    jobId: string;
    message: PublicQAMessage;
  } | null>(null);
  const [promptRevision, setPromptRevision] = useState(0);
  const [promptChecked, setPromptChecked] = useState<string | null>(null);
  const [replySending, setReplySending] = useState(false);
  const replySendingRef = useRef(false);
  const [replyError, setReplyError] = useState<unknown>(null);
  const [replyFieldError, setReplyFieldError] = useState<string | null>(null);
  const [acceptedReply, setAcceptedReply] = useState<PendingReply | null>(null);
  const [actions, setActions] = useState<Record<string, ActionEntry[]>>({});
  const [connection, setConnection] = useState<{
    state: ConnectionState;
    message?: string;
  }>({ state: "closed" });
  const [historyRevision, setHistoryRevision] = useState(0);
  const [resultRevision, setResultRevision] = useState(0);
  const [publishedJobs, setPublishedJobs] = useState<string[]>([]);
  const statusRead = useRef<AbortController | null>(null);
  const historyRead = useRef<AbortController | null>(null);
  const lifetime = useRef<AbortController | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    lifetime.current = controller;
    return () => controller.abort();
  }, []);
  const refreshHistory = useCallback(() => {
    historyRead.current?.abort();
    setHistoryRevision((n) => n + 1);
  }, []);
  const retry = useCallback(() => {
    setError(null);
    setReplyError(null);
    refreshHistory();
    setResultRevision((n) => n + 1);
    setPromptRevision((n) => n + 1);
  }, [refreshHistory]);
  const rememberJob = useCallback((job: QAJobView) => {
    setJobs((values) =>
      values[job.job_id] &&
      isTerminal(values[job.job_id]!.status) &&
      !isTerminal(job.status)
        ? values
        : { ...values, [job.job_id]: job },
    );
  }, []);

  useEffect(() => {
    if (!id) return;
    const controller = new AbortController();
    historyRead.current = controller;
    setLoading(true);
    setHistoryError(null);
    void readPage(id, undefined, controller.signal)
      .then((page) => {
        if (controller.signal.aborted) return;
        dispatch({ type: "history", messages: page.messages });
        if (firstHistory.current) {
          setBefore(page.next_before);
          firstHistory.current = false;
        }
        setHistoryReady(true);
        // Local acceptance refreshes this effect, aborting a pre-acceptance read.
        setActiveId(page.active_job_id);
      })
      .catch((failure) => {
        if (!controller.signal.aborted) setHistoryError(failure);
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [id, historyRevision]);

  useEffect(() => {
    if (!id) return;
    const saved = pendingRef.current;
    const root = saved && state.messages.find((m) => m.message_id === saved.message_id);
    if (saved && root?.payload.kind === "question") {
      setSubmitted({ ...saved, job_id: root.payload.job_id });
      if (!clearPendingQuestion(id)) setStorageWarning(true);
      pendingRef.current = null;
      setPending(null);
      setDraft((value) => (value === saved.question ? "" : value));
    }
    const reply = pendingReplyRef.current;
    if (reply && state.messages.some((m) =>
      m.message_id === reply.message_id &&
      m.reply_to_message_id === reply.prompt_message_id &&
      m.payload.kind === "clarification_reply",
    )) {
      if (!clearPendingReply(id)) setStorageWarning(true);
      pendingReplyRef.current = null;
      setPendingReply(null);
      setAcceptedReply(reply);
      setReplyError(null);
    }
  }, [id, state.messages]);

  const jobIds = [
    ...new Set([
      ...state.messages.flatMap((m) =>
        m.payload.kind === "question" &&
        !state.messages.some((answer) =>
          answer.reply_to_message_id === m.message_id && answer.payload.kind === "answer",
        ) ? [m.payload.job_id] : [],
      ),
      ...(submitted && !state.answers[submitted.job_id] ? [submitted.job_id] : []),
      ...(activeId ? [activeId] : []),
    ]),
  ]
    .sort()
    .join(",");
  useEffect(() => {
    if (!jobIds) return;
    const controller = new AbortController();
    for (const jobId of jobIds.split(",")) {
      void getQAJob(jobId, controller.signal)
        .then((job) => {
          if (!controller.signal.aborted) rememberJob(job);
        })
        .catch((failure) => {
          if (!controller.signal.aborted) setError(failure);
        });
    }
    return () => controller.abort();
  }, [jobIds, historyRevision, rememberJob]);

  const activeJob = activeId ? jobs[activeId] : undefined;
  const subscriptionId =
    activeId && activeJob && !isTerminal(activeJob.status) &&
    (activeJob.status !== "WAITING_INPUT" || promptChecked === activeId)
      ? activeId : null;
  const waitingId = activeJob?.status === "WAITING_INPUT" ? activeId : null;
  useEffect(() => {
    if (!waitingId) {
      setPrompt(null);
      setPromptChecked(null);
      return;
    }
    const controller = new AbortController();
    void getClarification(waitingId, controller.signal).then((message) => {
      if (controller.signal.aborted) return;
      dispatch({ type: "history", messages: [message] });
      setPrompt({ jobId: waitingId, message });
      setPromptChecked(waitingId);
    }).catch(async (failure: unknown) => {
      if (controller.signal.aborted) return;
      setPromptChecked(waitingId);
      if (failure instanceof ApiError && failure.business?.code === "QA_STATE_CONFLICT") {
        setPrompt(null);
        try {
          const current = await getQAJob(waitingId, controller.signal);
          if (!controller.signal.aborted) {
            rememberJob(current);
            refreshHistory();
          }
        } catch (readFailure) {
          if (!controller.signal.aborted) setReplyError(readFailure);
        }
      } else setReplyError(failure);
    });
    return () => controller.abort();
  }, [waitingId, promptRevision, rememberJob, refreshHistory]);
  useEffect(() => () => statusRead.current?.abort(), [subscriptionId]);
  const handlers = useMemo<EventHandlers<QAJobView>>(
    () => ({
      snapshot(value) {
        statusRead.current?.abort();
        rememberJob(value);
        if (value.status === "WAITING_INPUT") setPromptRevision((n) => n + 1);
        if (isTerminal(value.status)) refreshHistory();
      },
      status(value) {
        if (!subscriptionId) return;
        statusRead.current?.abort();
        setJobs((values) => {
          const previous = values[subscriptionId];
          return previous
            ? { ...values, [subscriptionId]: { ...previous, ...value } }
            : values;
        });
        if (value.status === "RETRY_WAIT") {
          const controller = new AbortController();
          statusRead.current = controller;
          void getQAJob(subscriptionId, controller.signal)
            .then((job) => {
              if (!controller.signal.aborted) rememberJob(job);
            })
            .catch((failure) => {
              if (!controller.signal.aborted) setError(failure);
            });
        }
        if (value.status === "WAITING_INPUT") setPromptRevision((n) => n + 1);
        if (isTerminal(value.status)) {
          refreshHistory();
          setResultRevision((n) => n + 1);
        }
      },
      progress(value, sequence) {
        if (subscriptionId)
          setActions((values) => ({
            ...values,
            [subscriptionId]: [
              ...(values[subscriptionId] ?? []),
              { ...value, sequence },
            ],
          }));
      },
      textDelta(delta) {
        if (subscriptionId)
          dispatch({ type: "delta", jobId: subscriptionId, delta });
      },
      clarification() {
        // A replayed prompt is only actionable after the current-prompt GET.
        refreshHistory();
        setPromptRevision((n) => n + 1);
      },
      answer() {
        if (subscriptionId)
          setPublishedJobs((values) =>
            values.includes(subscriptionId)
              ? values
              : [...values, subscriptionId],
          );
        refreshHistory();
        setResultRevision((n) => n + 1);
      },
      reset() {
        dispatch({ type: "reset-deltas" });
        if (subscriptionId)
          setActions((values) => ({ ...values, [subscriptionId]: [] }));
      },
      connection(status, message) {
        setConnection({ state: status, message });
      },
      storageUnavailable() {
        setStorageWarning(true);
      },
    }),
    [rememberJob, subscriptionId, refreshHistory],
  );
  const readSnapshot = useCallback(
    (signal: AbortSignal) => getQAJob(subscriptionId!, signal),
    [subscriptionId],
  );
  useJobEvents({
    kind: "qa",
    jobId: subscriptionId,
    decodeSnapshot: decodeQAJob,
    readSnapshot,
    handlers,
  });

  const succeeded = [
    ...new Set([
      ...publishedJobs,
      ...Object.values(jobs)
        .filter((job) => job.status === "SUCCEEDED")
        .map((job) => job.job_id),
    ]),
  ]
    .sort()
    .join(",");
  useEffect(() => {
    if (!succeeded) return;
    const controller = new AbortController();
    const timers = new Set<ReturnType<typeof setTimeout>>();
    async function load(jobId: string) {
      try {
        const answer = await getAnswer(jobId, controller.signal);
        if (controller.signal.aborted) return;
        if (answer) {
          dispatch({ type: "answer", jobId, answer });
          refreshHistory();
        } else
          timers.add(
            setTimeout(() => {
              void load(jobId);
            }, 1000),
          );
      } catch (failure) {
        if (!controller.signal.aborted) setError(failure);
      }
    }
    for (const jobId of succeeded.split(",")) void load(jobId);
    return () => {
      controller.abort();
      for (const timer of timers) clearTimeout(timer);
    };
  }, [succeeded, resultRevision, refreshHistory]);

  const busy =
    sending || replySending || (!!activeId && (!activeJob || !isTerminal(activeJob.status)));

  async function reply(text: string, retransmit = false) {
    const saved = pendingReplyRef.current;
    if (!id || replySendingRef.current || (!retransmit && !prompt)) return;
    const original = retransmit && saved ? saved.text : text;
    if (!original.trim() || Array.from(original).length > 10000) {
      setReplyFieldError(
        !original.trim() ? "请输入补充信息。" : "补充不能超过 10000 个 Unicode 字符。",
      );
      return;
    }
    const submission = retransmit && saved ? saved : {
      message_id: createUuid(),
      job_id: prompt!.jobId,
      prompt_message_id: prompt!.message.message_id,
      text: original,
    };
    replySendingRef.current = true;
    setReplySending(true);
    setReplyError(null);
    setReplyFieldError(null);
    pendingReplyRef.current = submission;
    setPendingReply(submission);
    if (!savePendingReply(id, submission)) setStorageWarning(true);
    try {
      const accepted = await replyToClarification(
        submission.job_id,
        submission.prompt_message_id,
        { message_id: submission.message_id, text: submission.text },
        lifetime.current?.signal,
      );
      if (lifetime.current?.signal.aborted) return;
      if (!clearPendingReply(id)) setStorageWarning(true);
      pendingReplyRef.current = null;
      setPendingReply(null);
      setAcceptedReply(submission);
      setPrompt(null);
      setActiveId(accepted.job_id);
      refreshHistory();
      const current = await getQAJob(accepted.job_id, lifetime.current?.signal);
      if (!lifetime.current?.signal.aborted) rememberJob(current);
    } catch (failure) {
      if (lifetime.current?.signal.aborted) return;
      setReplyError(failure);
      if (failure instanceof ApiError && !failure.outcomeUnknown) {
        if (!clearPendingReply(id)) setStorageWarning(true);
        pendingReplyRef.current = null;
        setPendingReply(null);
        setReplyFieldError(failure.fields.text ?? null);
        setPrompt(null);
        setPromptRevision((n) => n + 1);
      }
      refreshHistory();
    } finally {
      replySendingRef.current = false;
      if (!lifetime.current?.signal.aborted) setReplySending(false);
    }
  }
  async function send(kb: string | null, retransmit = false) {
    if (
      sendingRef.current ||
      busy ||
      !!pendingReplyRef.current ||
      !historyReady ||
      (!kb && !retransmit)
    )
      return;
    const saved = pendingRef.current;
    const text = retransmit && saved ? saved.question : draft;
    if (!text.trim() || Array.from(text).length > 10000) {
      setQuestionError(
        !text.trim() ? "请输入问题。" : "问题不能超过 10000 个 Unicode 字符。",
      );
      return;
    }
    setQuestionError(null);
    sendingRef.current = true;
    setSending(true);
    setError(null);
    let submission: PendingQuestion | null = null;
    try {
      let conversationId = id;
      if (!conversationId) {
        conversationId = (await createConversation(lifetime.current?.signal))
          .conversation_id;
        if (lifetime.current?.signal.aborted) return;
        setId(conversationId);
        onCreated(conversationId);
      }
      submission =
        saved &&
        saved.question === text &&
        saved.kb_id === (retransmit ? saved.kb_id : kb)
          ? saved
          : {
              conversation_id: conversationId,
              message_id: createUuid(),
              kb_id: kb!,
              question: text,
            };
      pendingRef.current = submission;
      if (!savePendingQuestion(submission)) setStorageWarning(true);
      const { message_id, kb_id, question } = submission;
      const accepted = await askQuestion(
        conversationId,
        {
          message_id,
          kb_id,
          question,
        },
        lifetime.current?.signal,
      );
      if (lifetime.current?.signal.aborted) return;
      setSubmitted({ message_id, kb_id, question, job_id: accepted.job_id });
      setActiveId(accepted.job_id);
      pendingRef.current = null;
      setPending(null);
      if (!clearPendingQuestion(conversationId)) setStorageWarning(true);
      setDraft((value) => (value === question ? "" : value));
      refreshHistory();
    } catch (failure) {
      if (lifetime.current?.signal.aborted) return;
      setError(failure);
      if (submission && failure instanceof ApiError && failure.outcomeUnknown) {
        setPending(submission);
        refreshHistory();
      } else if (
        submission &&
        failure instanceof ApiError &&
        failure.business?.code === "CONVERSATION_BUSY"
      ) {
        refreshHistory();
        pendingRef.current = null;
        setPending(null);
        if (!clearPendingQuestion(submission.conversation_id)) setStorageWarning(true);
      } else if (submission) {
        if (
          failure instanceof ApiError &&
          failure.business?.code === "QUESTION_MESSAGE_CONFLICT"
        )
          refreshHistory();
        pendingRef.current = null;
        setPending(null);
        if (!clearPendingQuestion(submission.conversation_id))
          setStorageWarning(true);
      }
    } finally {
      sendingRef.current = false;
      if (!lifetime.current?.signal.aborted) setSending(false);
    }
  }
  async function cancel(jobId: string) {
    if (cancelling) return;
    setCancelling(true);
    setError(null);
    try {
      const job = await cancelQAJob(jobId, lifetime.current?.signal);
      if (!lifetime.current?.signal.aborted) rememberJob(job);
    } catch (failure) {
      if (lifetime.current?.signal.aborted) return;
      setError(failure);
      try {
        rememberJob(await getQAJob(jobId, lifetime.current?.signal));
      } catch (readFailure) {
        if (!lifetime.current?.signal.aborted) setError(readFailure);
      }
    } finally {
      if (!lifetime.current?.signal.aborted) setCancelling(false);
    }
  }
  async function loadOlder() {
    if (!id || !before || loadingOlder) return;
    setLoadingOlder(true);
    try {
      const page = await readPage(id, before, lifetime.current!.signal);
      if (lifetime.current?.signal.aborted) return;
      dispatch({ type: "history", messages: page.messages });
      setBefore(page.next_before);
    } catch (failure) {
      if (!lifetime.current?.signal.aborted) setHistoryError(failure);
    } finally {
      if (!lifetime.current?.signal.aborted) setLoadingOlder(false);
    }
  }
  return {
    ...state,
    id,
    jobs,
    activeId,
    submitted,
    draft,
    setDraft,
    sending,
    busy,
    loading,
    historyReady,
    historyError,
    error,
    questionError,
    retry,
    actions,
    connection,
    send,
    cancel,
    cancelling,
    before,
    loadOlder,
    loadingOlder,
    pending,
    pendingReply,
    prompt: activeJob?.status === "WAITING_INPUT" && prompt?.jobId === activeId ? prompt.message : null,
    reply,
    replySending,
    replyError,
    replyFieldError,
    acceptedReply,
    storageWarning,
  };
}
