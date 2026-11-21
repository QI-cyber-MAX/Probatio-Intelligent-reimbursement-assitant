import { useEffect, useRef, useState } from "react";
import {
  isTerminal,
  type DocumentSourceView,
  type IngestionAccepted,
  type IngestionJobView,
} from "../../api/contracts";
import {
  cancelIngestion,
  getIngestionJob,
  rebuildIngestion,
} from "../../api/documents";
import { ApiError } from "../../api/http";
import {
  clearPendingRebuild,
  readPendingRebuild,
  savePendingRebuild,
} from "../../storage/pending-submissions";
import { ErrorNotice } from "../../ui/ErrorNotice";
import { Icon } from "../../ui/Icon";
import { StatusBadge } from "../../ui/StatusBadge";
import { createUuid } from "../../uuid";
import type { Tracking } from "./useIngestionJobs";
import { SourceMetadataForm } from "./SourceMetadataForm";
import { stageLabels, statusLabels } from "./job-labels";

const dateTime = (value: string) =>
  new Intl.DateTimeFormat("zh-CN", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));

export function SourceStatusCard({
  documentId,
  title,
  source,
  tracking,
  highlighted,
  onAccepted,
  onJob,
  reload,
  storageUnavailable,
}: {
  documentId: string;
  title: string;
  source: DocumentSourceView;
  tracking?: Tracking;
  highlighted: boolean;
  onAccepted(value: IngestionAccepted): void;
  onJob(view: IngestionJobView): void;
  reload(): void;
  storageUnavailable(): void;
}) {
  const view = { ...source.latest_job, ...tracking?.view, ...tracking?.status };
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [editing, setEditing] = useState(false);
  const [detailsOpen, setDetailsOpen] = useState(false);
  const [restored] = useState(() => {
    let unavailable = false;
    const value = readPendingRebuild(documentId, source.source_hash, () => { unavailable = true; });
    return { value, unavailable };
  });
  const [pending, setPending] = useState(restored.value);
  const sourceHash = source.source_hash;
  const detailsId = `task-${documentId}-${sourceHash}`;
  const pendingId = pending?.new_job_id;
  const lifetime = useRef<AbortController | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    lifetime.current = controller;
    return () => controller.abort();
  }, []);
  useEffect(() => {
    if (restored.unavailable) storageUnavailable();
  }, [restored.unavailable, storageUnavailable]);
  useEffect(() => {
    if (!pendingId) return;
    const controller = new AbortController();
    void getIngestionJob(pendingId, controller.signal)
      .then((value) => {
        if (controller.signal.aborted) return;
        if (!clearPendingRebuild(documentId, sourceHash)) storageUnavailable();
        setPending(null);
        onJob(value);
        reload();
      })
      .catch((failure) => {
        if (
          !controller.signal.aborted &&
          !(failure instanceof ApiError && failure.status === 404)
        )
          setError(failure);
      });
    return () => controller.abort();
  }, [pendingId, documentId, sourceHash, onJob, reload, storageUnavailable]);
  function clearPending() {
    if (!clearPendingRebuild(documentId, sourceHash)) storageUnavailable();
    setPending(null);
  }
  async function checkPending() {
    if (!pending || busy) return;
    setBusy(true);
    setError(null);
    const signal = lifetime.current!.signal;
    try {
      const value = await getIngestionJob(pending.new_job_id, signal);
      if (signal.aborted) return;
      clearPending();
      onJob(value);
      reload();
    } catch (failure) {
      if (!signal.aborted) setError(failure);
    } finally {
      if (!signal.aborted) setBusy(false);
    }
  }
  async function rebuild() {
    if (busy) return;
    const signal = lifetime.current!.signal;
    const submission = pending ?? {
      old_job_id: view.job_id,
      new_job_id: createUuid(),
      document_id: documentId,
      source_hash: sourceHash,
    };
    setPending(submission);
    if (!savePendingRebuild(submission)) storageUnavailable();
    setBusy(true);
    setError(null);
    try {
      const accepted = await rebuildIngestion(
        submission.old_job_id,
        submission.new_job_id,
        signal,
      );
      if (signal.aborted) return;
      clearPending();
      onAccepted(accepted);
    } catch (failure) {
      if (signal.aborted) return;
      if (failure instanceof ApiError && failure.outcomeUnknown) {
        try {
          const value = await getIngestionJob(submission.new_job_id, signal);
          if (signal.aborted) return;
          clearPending();
          onJob(value);
          reload();
        } catch {
          if (!signal.aborted) setError(failure);
        }
      } else {
        setError(failure);
        if (
          failure instanceof ApiError &&
          failure.business?.code === "INGESTION_SOURCE_BUSY"
        )
          reload();
        else clearPending();
      }
    } finally {
      if (!signal.aborted) setBusy(false);
    }
  }
  async function cancel() {
    if (busy) return;
    const signal = lifetime.current!.signal;
    setBusy(true);
    setError(null);
    try {
      const job = await cancelIngestion(view.job_id, signal);
      if (!signal.aborted) onJob(job);
    } catch (failure) {
      if (signal.aborted) return;
      if (failure instanceof ApiError && failure.outcomeUnknown) {
        try {
          const job = await getIngestionJob(view.job_id, signal);
          if (!signal.aborted) onJob(job);
        } catch {
          if (!signal.aborted) setError(failure);
        }
      } else setError(failure);
    } finally {
      if (!signal.aborted) setBusy(false);
    }
  }
  const tone =
    view.status === "SUCCEEDED"
      ? "success"
      : view.status === "FAILED"
        ? "danger"
        : view.status === "RETRY_WAIT" || view.status === "WAITING_INPUT"
          ? "warning"
          : "neutral";
  const policy = source.policy_metadata;
  return (
    <section
      className={`source-card ${highlighted ? "highlighted" : ""}`}
      id={`source-${documentId}-${sourceHash}`}
      tabIndex={-1}
      aria-label={`${title} 原件 ${sourceHash.slice(0, 8)}`}
    >
      <div className="source-identity">
        <Icon name="document" />
        <div>
          <p className="source-code">
            原件 <code title={sourceHash}>{sourceHash.slice(0, 12)}</code>
          </p>
          <p className="help">上传于 {dateTime(source.uploaded_at)}</p>
        </div>
      </div>
      <div className="source-period">
        <span className="cell-label">
          {policy.version_status === "CURRENT" ? "新版本" : "历史版本"} · 生效区间
        </span>
        <p>
          {policy.effective_from ? (
            <>
              <time dateTime={policy.effective_from.value}>{policy.effective_from.value}</time>{" "}
              <span className="help" title={policy.effective_from.source === "USER" ? "用户登记" : "原文提取"}>
                {policy.effective_from.source === "USER" ? "人工" : "提取"}
              </span>
            </>
          ) : "生效日期未知"}
        </p>
        <p className="help">
          {policy.effective_to ? `至 ${policy.effective_to.value} 前` : "未设截止日"}
        </p>
      </div>
      <div className="source-index">
        <span className="cell-label">索引可用性</span>
        <StatusBadge
          tone={source.serving_generation_id ? "success" : "neutral"}
        >
          {source.serving_generation_id ? "索引可用" : "尚未发布"}
        </StatusBadge>
      </div>
      <div className="source-job">
        <span className="cell-label">最近任务</span>
        <StatusBadge tone={tone}>{statusLabels[view.status]}</StatusBadge>
        <p className="help">
          {isTerminal(view.status)
            ? `结束阶段 · ${stageLabels[view.stage]}`
            : `处理阶段 · ${stageLabels[view.stage]}`}
        </p>
      </div>
      <div className="source-actions">
        {!isTerminal(view.status) && (
          <button
            type="button"
            className="text-button danger-text"
            disabled={busy || view.status === "CANCELLING"}
            onClick={() => void cancel()}
          >
            {busy ? "正在提交…" : "取消任务"}
          </button>
        )}
        {isTerminal(view.status) &&
          !pending && (
            <button
              type="button"
              className="button secondary compact"
              disabled={busy}
              onClick={() => void rebuild()}
            >
              <Icon name="refresh" />
              {busy ? "正在提交…" : "重新处理"}
            </button>
          )}
        <div className="inline-actions">
          {isTerminal(view.status) && (
            <button
              type="button"
              className="text-button"
              disabled={busy || !!pending}
              onClick={() => setEditing(true)}
            >
              修改元数据
            </button>
          )}
        <button
          type="button"
          className="text-button task-toggle"
          aria-expanded={detailsOpen}
          aria-controls={detailsOpen ? detailsId : undefined}
          onClick={() => setDetailsOpen((open) => !open)}
        >
          任务详情{tracking?.progress.length ? ` · ${tracking.progress.length}` : ""}
        </button>
        </div>
      </div>
      {editing && <SourceMetadataForm documentId={documentId} sourceHash={sourceHash} policy={policy} onClose={() => setEditing(false)} onAccepted={onAccepted} onJob={onJob} reload={reload} storageUnavailable={storageUnavailable} />}
      <div className="source-extra">
        {view.status === "RETRY_WAIT" && (
          <p className="notice warning">
            自动重试 {view.retry_count} / {view.max_retries}
            {view.next_retry_at
              ? ` · 下次安排于 ${dateTime(view.next_retry_at)}`
              : ""}
          </p>
        )}
        {(view.terminal_reason || view.last_error_code) && (
          <p
            className={`source-reason ${view.status === "FAILED" ? "danger-text" : "muted"}`}
          >
            {view.terminal_reason ?? "服务端未提供进一步说明"}
            {view.last_error_code && <code>{view.last_error_code}</code>}
          </p>
        )}
        {tracking?.message && (
          <p className="notice warning">{tracking.message}</p>
        )}
        {tracking?.connection === "connecting" && (
          <p className="help">正在连接处理记录…</p>
        )}
        {pending && (
          <div className="notice warning">
            <p>本次重建待核对，重传将沿用同一任务 ID。</p>
            <div className="inline-actions">
              <button
                className="text-button"
                disabled={busy}
                onClick={() => void checkPending()}
              >
                核对重建结果
              </button>
              <button
                className="text-button"
                disabled={busy}
                onClick={() => void rebuild()}
              >
                重传本次重建
              </button>
            </div>
          </div>
        )}
        {error !== null && <ErrorNotice error={error} />}
        {detailsOpen && (
          <div className="task-details" id={detailsId}>
            <dl>
              <dt>完整原件摘要</dt>
              <dd>
                <code>{sourceHash}</code>
              </dd>
              <dt>最近任务 ID</dt>
              <dd>
                <code>{view.job_id}</code>
              </dd>
              <dt>服务代次</dt>
              <dd>
                <code>{source.serving_generation_id ?? "尚未发布"}</code>
              </dd>
              <dt>自动重试次数</dt>
              <dd>
                {view.retry_count} / {view.max_retries}
              </dd>
            </dl>
            {tracking?.progress.length ? (
              <ol className="timeline">
                {tracking.progress.map((entry) => (
                  <li key={entry.sequence}>{entry.value.text}</li>
                ))}
              </ol>
            ) : (
              <p className="help">
                {isTerminal(view.status)
                  ? "本页未载入历史动作。"
                  : "已恢复任务，等待后续处理记录。"}
              </p>
            )}
          </div>
        )}
      </div>
    </section>
  );
}
