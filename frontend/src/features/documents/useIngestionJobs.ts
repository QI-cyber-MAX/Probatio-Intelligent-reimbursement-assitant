import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  isTerminal,
  type DocumentView,
  type IngestionJobView,
  type JobProgress,
  type JobStatusEvent,
} from "../../api/contracts";
import { getIngestionJob } from "../../api/documents";
import { errorMessage } from "../../api/http";
import type { ConnectionState } from "../../api/job-events";

export interface Tracking {
  view?: IngestionJobView;
  status?: JobStatusEvent;
  progress: { sequence: number; value: JobProgress }[];
  connection?: ConnectionState;
  message?: string;
}
export function useIngestionJobs(
  documents: DocumentView[],
  priority: string[],
  reload: () => void,
  storageUnavailable: () => void,
) {
  const [tracking, setTracking] = useState<Record<string, Tracking>>({});
  const completed = useRef(new Set<string>());
  const patch = useCallback(
    (id: string, update: (current: Tracking) => Tracking) => {
      setTracking((previous) => ({
        ...previous,
        [id]: update(previous[id] ?? { progress: [] }),
      }));
    },
    [],
  );
  const finish = useCallback(
    (id: string, status: JobStatusEvent) => {
      if (isTerminal(status.status) && !completed.current.has(id)) {
        completed.current.add(id);
        reload();
      }
    },
    [reload],
  );
  const snapshot = useCallback(
    (value: IngestionJobView) => {
      patch(value.job_id, (previous) => {
        if (
          previous.status &&
          isTerminal(previous.status.status) &&
          !isTerminal(value.status)
        )
          return previous;
        return { ...previous, view: value, status: value };
      });
      finish(value.job_id, value);
    },
    [finish, patch],
  );
  const status = useCallback(
    (id: string, value: JobStatusEvent) => {
      patch(id, (previous) => ({ ...previous, status: value }));
      finish(id, value);
    },
    [finish, patch],
  );
  const progress = useCallback(
    (id: string, value: JobProgress, sequence: number) => {
      patch(id, (previous) => ({
        ...previous,
        progress: [...previous.progress, { sequence, value }],
      }));
    },
    [patch],
  );
  const connection = useCallback(
    (id: string, state: ConnectionState, message?: string) => {
      patch(id, (previous) => ({ ...previous, connection: state, message }));
    },
    [patch],
  );
  const reset = useCallback(
    (id: string) => patch(id, (previous) => ({ ...previous, progress: [] })),
    [patch],
  );
  const jobs = useMemo(
    () =>
      [...new Map([
        ...Object.values(tracking).flatMap((entry) => entry.view ? [entry.view] : []),
        ...documents.flatMap((doc) => doc.versions.map((source) => source.latest_job)),
      ].map((initial) => {
        const current = tracking[initial.job_id];
        return [initial.job_id, { ...initial, ...current?.view, ...current?.status }] as const;
      })).values()],
    [documents, tracking],
  );
  const active = jobs
    .filter((job) => !isTerminal(job.status))
    .sort((a, b) => {
      const rank = (id: string) =>
        priority.includes(id) ? priority.indexOf(id) : priority.length;
      return rank(a.job_id) - rank(b.job_id);
    });
  const subscribed = active.slice(0, 3).map((job) => job.job_id);
  // A stable scalar keeps progress/connection rendering from restarting the polling loop.
  const overflowKey = active
    .slice(3)
    .map((job) => job.job_id)
    .join(",");
  useEffect(() => {
    if (!overflowKey) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      const ids = overflowKey.split(",");
      let next = 0;
      await Promise.all(Array.from({ length: Math.min(4, ids.length) }, async () => {
        while (next < ids.length && !controller.signal.aborted) {
          const id = ids[next++]!;
          try {
            const value = await getIngestionJob(id, controller.signal);
            if (!controller.signal.aborted) {
              snapshot(value);
              connection(id, "closed");
            }
          } catch (error) {
            if (!controller.signal.aborted) connection(id, "recovering", errorMessage(error));
          }
        }
      }));
      if (!controller.signal.aborted) timer = setTimeout(poll, 5000);
    };
    void poll();
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [overflowKey, snapshot, connection]);
  return {
    tracking,
    subscribed,
    snapshot,
    status,
    progress,
    connection,
    reset,
    storageUnavailable,
  };
}
