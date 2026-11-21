import { useCallback, useEffect, useRef, useState } from "react";
import {
  getAcceptedJob,
  getDocument,
  listDocuments,
} from "../../api/documents";
import {
  isTerminal,
  type DocumentItem,
  type DocumentView,
  type IngestionAccepted,
  type IngestionJobView,
  type KnowledgeBaseItem,
} from "../../api/contracts";
import { ErrorNotice } from "../../ui/ErrorNotice";
import { Icon } from "../../ui/Icon";
import { DocumentList } from "./DocumentList";
import { useIngestionJobs } from "./useIngestionJobs";
import { IngestionSubscription } from "./IngestionSubscription";
import { UploadPolicyForm } from "./UploadPolicyForm";
import { BatchUploadPolicyForm } from "./BatchUploadPolicyForm";
import { usePolicyUploadQueue } from "./usePolicyUploadQueue";
import { statusLabels } from "./job-labels";

export function LibraryPage({
  base,
  navigateToChat,
  setLeaveGuard,
}: {
  base: KnowledgeBaseItem;
  navigateToChat(): void;
  setLeaveGuard(guard: (() => boolean) | null): void;
}) {
  const [candidates, setCandidates] = useState<DocumentItem[]>([]);
  const [documents, setDocuments] = useState<DocumentView[]>([]);
  const [failures, setFailures] = useState<Record<string, unknown>>({});
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);
  const refreshRequest = useRef<(() => void) | null>(null);
  const [uploading, setUploading] = useState(false);
  const [accepted, setAccepted] = useState<IngestionAccepted | null>(null);
  const [receiptErrors, setReceiptErrors] = useState<Record<string, unknown>>({});
  const receiptReads = useRef(new Map<string, AbortController>());
  const [batchOpen, setBatchOpen] = useState(false);
  const [priority, setPriority] = useState<string[]>([]);
  const [announcement, setAnnouncement] = useState("");
  const [storageWarning, setStorageWarning] = useState(false);
  const located = useRef<string | null>(null);
  const reload = useCallback(() => refreshRequest.current?.(), []);
  const storageUnavailable = useCallback(() => setStorageWarning(true), []);
  useEffect(() => {
    window.addEventListener("focus", reload);
    return () => window.removeEventListener("focus", reload);
  }, [reload]);
  useEffect(() => {
    const controller = new AbortController();
    let inFlight = false;
    let reread = false;
    async function load() {
      if (inFlight) { reread = true; return; }
      inFlight = true;
      do {
        reread = false;
        setLoading(true);
        setError(null);
        try {
          const items = await listDocuments(base.kb_id, controller.signal);
          if (controller.signal.aborted) return;
          setCandidates(items);
          setFailures({});
          setDocuments((previous) =>
            previous.filter((doc) =>
              items.some((item) => item.document_id === doc.document_id),
            ),
          );
          let next = 0;
          await Promise.all(
            Array.from({ length: Math.min(4, items.length) }, async () => {
              while (next < items.length && !controller.signal.aborted) {
                const item = items[next++]!;
                try {
                  const doc = await getDocument(
                    item.document_id,
                    controller.signal,
                  );
                  if (!controller.signal.aborted)
                    setDocuments((previous) => [
                      ...previous.filter(
                        (value) => value.document_id !== doc.document_id,
                      ),
                      doc,
                    ]);
                } catch (failure) {
                  if (!controller.signal.aborted)
                    setFailures((previous) => ({
                      ...previous,
                      [item.document_id]: failure,
                    }));
                }
              }
            }),
          );
        } catch (failure) {
          if (!controller.signal.aborted) setError(failure);
        } finally {
          if (!controller.signal.aborted) setLoading(false);
        }
      } while (reread && !controller.signal.aborted);
      inFlight = false;
    }
    refreshRequest.current = () => { void load(); };
    void load();
    return () => { controller.abort(); refreshRequest.current = null; };
  }, [base.kb_id]);
  const jobs = useIngestionJobs(
    documents,
    priority,
    reload,
    storageUnavailable,
  );
  const { snapshot } = jobs;
  const onJob = useCallback(
    (view: IngestionJobView) => {
      snapshot(view);
      setPriority((previous) => [
        view.job_id,
        ...previous.filter((id) => id !== view.job_id),
      ]);
      reload();
      setAnnouncement(`任务状态已更新：${statusLabels[view.status]}`);
    },
    [snapshot, reload],
  );
  useEffect(() => {
    const reads = receiptReads.current;
    return () => { for (const controller of reads.values()) controller.abort(); reads.clear(); };
  }, []);
  const readReceipt = useCallback((value: IngestionAccepted) => {
    if (receiptReads.current.has(value.job_id)) return;
    const controller = new AbortController();
    receiptReads.current.set(value.job_id, controller);
    void getAcceptedJob(value, controller.signal).then((view) => {
      if (!controller.signal.aborted) {
        snapshot(view);
        setReceiptErrors((previous) => { const next = { ...previous }; delete next[value.job_id]; return next; });
      }
    }).catch((failure) => {
      if (!controller.signal.aborted) setReceiptErrors((previous) => ({ ...previous, [value.job_id]: failure }));
    }).finally(() => { if (receiptReads.current.get(value.job_id) === controller) receiptReads.current.delete(value.job_id); });
  }, [snapshot]);
  const recordAccepted = useCallback((value: IngestionAccepted) => {
    setPriority((previous) => [value.job_id, ...previous.filter((id) => id !== value.job_id)]);
    setAnnouncement("原件已受理，正在读取处理状态。");
    readReceipt(value);
    reload();
  }, [readReceipt, reload]);
  const locateAccepted = useCallback((value: IngestionAccepted) => {
    located.current = null;
    setAccepted(value);
  }, []);
  const onAccepted = useCallback((value: IngestionAccepted) => {
    recordAccepted(value);
    setUploading(false);
    locateAccepted(value);
  }, [recordAccepted, locateAccepted]);
  const queue = usePolicyUploadQueue(base.kb_id, candidates, recordAccepted);
  useEffect(() => {
    setLeaveGuard(() => !queue.hasPending || window.confirm(
      `离开将结束本页文件队列；已受理任务继续处理，其余文件需重新选择。${queue.running ? "当前上传结果可能未确认，请在原件列表核对。" : ""}确定离开？`,
    ));
    const beforeUnload = (event: BeforeUnloadEvent) => { if (queue.hasPending) { event.preventDefault(); event.returnValue = ""; } };
    window.addEventListener("beforeunload", beforeUnload);
    return () => { setLeaveGuard(null); window.removeEventListener("beforeunload", beforeUnload); };
  }, [queue.hasPending, queue.running, setLeaveGuard]);
  const acceptedId = accepted?.job_id;
  useEffect(() => {
    if (!accepted || located.current === accepted.job_id) return;
    const element = document.getElementById(
      `source-${accepted.document_id}-${accepted.source_hash}`,
    );
    if (element) {
      element.scrollIntoView({ block: "center", behavior: "instant" });
      element.focus({ preventScroll: true });
      located.current = accepted.job_id;
    }
  }, [accepted, documents]);
  const allSources = documents.flatMap((doc) => doc.versions);
  const available = allSources.filter(
    (source) => source.serving_generation_id,
  ).length;
  const active = allSources.filter(
    (source) =>
      !isTerminal(
        (jobs.tracking[source.latest_job.job_id]?.status ?? source.latest_job)
          .status,
      ),
  ).length;
  const latestStatus = allSources
    .map(
      (source) =>
        `${source.source_hash.slice(0, 8)} ${statusLabels[jobs.tracking[source.latest_job.job_id]?.status?.status ?? source.latest_job.status]}`,
    )
    .join("；");
  return (
    <>
      <div className="page-heading">
        <div>
          <h1 tabIndex={-1} id="page-title">
            知识库与制度
          </h1>
        </div>
        <div className="upload-actions">
        <button type="button" className="button secondary" disabled={(!candidates.length && loading) || error !== null} onClick={() => setBatchOpen(true)}>
          批量上传制度{queue.rows.length ? `（${queue.rows.length}）` : ""}
        </button>
        <button
          type="button"
          className="button primary"
          disabled={loading || error !== null}
          onClick={() => setUploading(true)}
        >
          <Icon name="upload" />
          上传制度
        </button>
        </div>
      </div>
      <div className="library-summary">
        <div>
          <div>
            <h2>{base.name}</h2>
          </div>
        </div>
        <dl className="library-stats">
          <div>
            <dt>制度</dt>
            <dd>{candidates.length.toString().padStart(2, "0")}</dd>
          </div>
          <div>
            <dt>索引可用</dt>
            <dd>{available.toString().padStart(2, "0")}</dd>
          </div>
          <div>
            <dt>处理中</dt>
            <dd>{active.toString().padStart(2, "0")}</dd>
          </div>
        </dl>
      </div>
      <div className="list-toolbar">
        <div>
          <span className="small-square" aria-hidden="true" />
          全部制度与原件
          <span className="muted">{allSources.length} 份原件</span>
        </div>
        <button
          className="text-button"
          type="button"
          disabled={loading}
          onClick={reload}
        >
          <Icon name="refresh" />
          {loading ? "正在读取…" : "刷新"}
        </button>
      </div>
      {storageWarning && (
        <p className="notice warning">
          浏览器存储不可用；本页继续跟踪任务，刷新后未确认重建的身份可能无法恢复。
        </p>
      )}
      {accepted && (
        <p className="notice" role="status">
          <Icon name="check" />
          原件已受理 · 任务 <code>{acceptedId?.slice(0, 8)}</code>
          ；处理结果以下方原件状态为准。
        </p>
      )}
      {accepted && receiptErrors[accepted.job_id] !== undefined && (
        <ErrorNotice error={receiptErrors[accepted.job_id]} retry={() => readReceipt(accepted)} />
      )}
      {error !== null && <ErrorNotice error={error} retry={reload} />}
      {loading && !candidates.length && (
        <div className="source-skeleton" aria-label="正在读取制度列表" />
      )}
      {!loading && !error && !candidates.length && (
        <div className="empty-state">
          <div className="empty-document">
            <Icon name="document" />
          </div>
          <p className="eyebrow">YOUR POLICY ARCHIVE STARTS HERE</p>
          <h2>上传第一份制度</h2>
          <p className="muted">
            将制度原件登记到「{base.name}」，发布后即可用于问答。
          </p>
          <button
            type="button"
            className="button primary"
            onClick={() => setUploading(true)}
          >
            <Icon name="plus" />
            上传第一份制度
          </button>
        </div>
      )}
      <DocumentList
        candidates={candidates}
        documents={documents}
        failures={failures}
        tracking={jobs.tracking}
        selectedHash={
          accepted ? `${accepted.document_id}:${accepted.source_hash}` : null
        }
        onRetry={reload}
        onAccepted={onAccepted}
        onJob={onJob}
        reload={reload}
        storageUnavailable={storageUnavailable}
      />
      {available > 0 && (
        <div className="archive-footer">
          <p>
            <Icon name="check" />
            已有 {available} 份原件的索引可用
          </p>
          <button
            type="button"
            className="button secondary"
            onClick={navigateToChat}
          >
            前往制度问答
            <Icon name="arrow" />
          </button>
        </div>
      )}
      {jobs.subscribed.map((id) => (
        <IngestionSubscription key={id} id={id} jobs={jobs} />
      ))}
      {batchOpen && <BatchUploadPolicyForm base={base} candidates={candidates} queue={queue} tracking={jobs.tracking} receiptErrors={receiptErrors}
        onClose={() => setBatchOpen(false)} onReload={reload} onRead={readReceipt}
        onView={(value) => { setBatchOpen(false); locateAccepted(value); reload(); }} />}
      {uploading && (
        <UploadPolicyForm
          base={base}
          candidates={candidates}
          onClose={() => setUploading(false)}
          onAccepted={onAccepted}
          onReload={reload}
        />
      )}
      <p className="sr-only" aria-live="polite" aria-atomic="true">
        {announcement} {latestStatus && `原件任务状态：${latestStatus}`}
      </p>
    </>
  );
}
