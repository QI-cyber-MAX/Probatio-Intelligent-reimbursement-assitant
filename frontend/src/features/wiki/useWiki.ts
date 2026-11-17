import { useCallback, useEffect, useState } from "react";
import type {
  WikiEditCreate,
  WikiJob,
  WikiVersion,
  WikiWorkspace,
} from "../../api/contracts";
import { ApiError } from "../../api/http";
import { getWiki, getWikiJob, submitWiki } from "../../api/wiki";
import { createUuid } from "../../uuid";
import {
  clearWikiDraft,
  readWikiDraft,
  saveWikiDraft,
  type WikiDraft,
} from "../../storage/pending-submissions";

export function editVersion(version: WikiVersion): WikiEditCreate {
  const pages = Object.values(version.pages);
  const scenes = version.pages.overview!.child_page_ids.map(
    (id) => version.pages[id]!,
  );
  return {
    new_job_id: createUuid(),
    base_job_id: version.job_id,
    source_pages: pages
      .filter((p) => p.kind === "document")
      .map((p) => ({ page_id: p.page_id, statements: p.statements })),
    scenes: scenes.map((p) => ({
      scene_ref: p.page_id,
      page_id: p.page_id,
      title: p.title,
      source_page_ids: p.child_page_ids,
      statements: p.statements,
    })),
    overview: {
      business_context: {
        text: version.pages.overview!.statements.find(
          (s) => s.topic === "business_context",
        )!.text,
        related_scene_refs: version.pages.overview!.statements.find(
          (s) => s.topic === "business_context",
        )!.related_page_ids,
      },
      scenarios: scenes.map((p) => ({
        scene_ref: p.page_id,
        text: version.pages.overview!.statements.find(
          (s) =>
            s.topic === "relationship" && s.related_page_ids[0] === p.page_id,
        )!.text,
      })),
    },
  };
}
const terminal = (job: WikiJob) =>
  ["SUCCEEDED", "FAILED", "CANCELLED"].includes(job.status);
export function useWiki(kb: string) {
  const [restored] = useState(() => {
    let failed = false;
    const value = readWikiDraft(kb, () => {
      failed = true;
    });
    return { value, failed };
  });
  const [draft, setDraft] = useState<WikiDraft | null>(restored.value);
  const [storageWarning, setStorageWarning] = useState(restored.failed);
  const [workspace, setWorkspace] = useState<WikiWorkspace | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [readError, setReadError] = useState<unknown>(null);
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [job, setJob] = useState<WikiJob | null>(null);
  const jobId = draft?.submitted ? draft.request.new_job_id : null;
  const persist = useCallback(
    (value: WikiDraft | null) => {
      setDraft(value);
      if (!(value ? saveWikiDraft(kb, value) : clearWikiDraft(kb)))
        setStorageWarning(true);
    },
    [kb],
  );
  const refresh = useCallback(
    async (signal?: AbortSignal) => {
      try {
        const current = await getWiki(kb, signal);
        if (signal?.aborted) return;
        setWorkspace(current);
        if (jobId) {
          try {
            const result = await getWikiJob(jobId, signal);
            if (signal?.aborted) return;
            setJob(result);
            if (terminal(result)) {
              if (result.status === "SUCCEEDED" && result.is_current) {
                persist(null);
                setNotice("知识导航已发布。");
                setWorkspace(await getWiki(kb, signal));
              } else {
                setDraft((previous) => {
                  if (!previous) return previous;
                  const retained = {
                    request: {
                      ...previous.request,
                      new_job_id: createUuid(),
                    },
                    submitted: false,
                  };
                  if (!saveWikiDraft(kb, retained)) setStorageWarning(true);
                  return retained;
                });
                setNotice(
                  result.last_error_code === "WIKI_SOURCE_REVISION_CONFLICT"
                    ? "来源已更新，本次内容未发布。"
                    : (result.terminal_reason ??
                        "任务已结束，此版本不是当前发布版本。"),
                );
              }
            }
          } catch (failure) {
            if (!(failure instanceof ApiError && failure.status === 404))
              throw failure;
            setNotice("尚未查到提交任务，可使用原提交标识重试。");
          }
        }
        setReadError(null);
      } catch (failure) {
        if (!signal?.aborted) setReadError(failure);
      }
    },
    [kb, jobId, persist],
  );
  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      await refresh(controller.signal);
      if (!controller.signal.aborted)
        timer = setTimeout(() => {
          void poll();
        }, 2000);
    };
    void poll();
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [refresh]);
  const stale =
    !!draft &&
    !!workspace?.published_version &&
    draft.request.base_job_id !== workspace.published_version.job_id;
  const sourcesStale =
    !!workspace?.published_version &&
    workspace.current_revision !==
      workspace.published_version.snapshot_revision;
  const locked =
    !!workspace?.active_job ||
    sourcesStale ||
    stale ||
    busy ||
    !!draft?.submitted ||
    readError !== null ||
    !workspace;
  const update = (request: WikiEditCreate) => {
    setError(null);
    persist({ request, submitted: false });
  };
  const start = async () => {
    setBusy(true);
    try {
      const latest = await getWiki(kb);
      setWorkspace(latest);
      if (
        latest.active_job ||
        !latest.published_version ||
        latest.current_revision !== latest.published_version.snapshot_revision
      )
        return;
      persist({
        request: editVersion(latest.published_version),
        submitted: false,
      });
      setNotice("");
      setJob(null);
      setError(null);
    } catch (failure) {
      setError(failure);
    } finally {
      setBusy(false);
    }
  };
  const save = async () => {
    if (!draft) return;
    setBusy(true);
    setError(null);
    const value = { ...draft, submitted: true };
    persist(value);
    try {
      if (draft.submitted) {
        try {
          const existing = await getWikiJob(draft.request.new_job_id);
          setJob(existing);
          await refresh();
          return;
        } catch (failure) {
          if (!(failure instanceof ApiError && failure.status === 404))
            throw failure;
        }
      }
      await submitWiki(kb, draft.request);
      setNotice("发布任务已受理，正在核对发布结果。");
    } catch (failure) {
      setError(failure);
      if (failure instanceof ApiError && !failure.outcomeUnknown)
        persist({ ...draft, submitted: false });
    } finally {
      setBusy(false);
    }
  };
  return {
    workspace,
    draft: draft?.request ?? null,
    pending: !!draft?.submitted,
    storageWarning,
    error: error ?? readError,
    notice,
    job,
    busy,
    locked,
    stale,
    sourcesStale,
    update,
    start,
    save,
    discard: () => {
      persist(null);
      setNotice("");
      setError(null);
    },
    refresh,
  };
}
