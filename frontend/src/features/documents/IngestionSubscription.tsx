import { useCallback, useEffect, useMemo, useRef } from "react";
import {
  decodeIngestionJob,
  isTerminal,
  type IngestionJobView,
} from "../../api/contracts";
import { getIngestionJob } from "../../api/documents";
import { errorMessage } from "../../api/http";
import type { EventHandlers } from "../../api/job-events";
import { useJobEvents } from "../../api/useJobEvents";
import type { useIngestionJobs } from "./useIngestionJobs";

export function IngestionSubscription({
  id,
  jobs,
}: {
  id: string;
  jobs: ReturnType<typeof useIngestionJobs>;
}) {
  const { snapshot, status, progress, connection, reset, storageUnavailable } =
    jobs;
  const read = useRef<AbortController | null>(null);
  useEffect(() => () => read.current?.abort(), []);
  const calibrate = useCallback(() => {
    read.current?.abort();
    const controller = new AbortController();
    read.current = controller;
    void getIngestionJob(id, controller.signal)
      .then((value) => {
        if (!controller.signal.aborted) {
          snapshot(value);
          connection(id, "connected");
        }
      })
      .catch((error) => {
        if (!controller.signal.aborted)
          connection(id, "recovering", `状态读取失败：${errorMessage(error)}`);
      });
  }, [id, snapshot, connection]);
  const handlers = useMemo<EventHandlers<IngestionJobView>>(
    () => ({
      snapshot,
      status: (value) => {
        status(id, value);
        if (!isTerminal(value.status)) calibrate();
      },
      progress: (value, sequence) => {
        progress(id, value, sequence);
        calibrate();
      },
      connection: (state, message) => connection(id, state, message),
      reset: () => reset(id),
      storageUnavailable,
    }),
    [
      id,
      snapshot,
      status,
      progress,
      connection,
      reset,
      storageUnavailable,
      calibrate,
    ],
  );
  const readSnapshot = useCallback(
    (signal: AbortSignal) => getIngestionJob(id, signal),
    [id],
  );
  useJobEvents({
    kind: "ingestion",
    jobId: id,
    decodeSnapshot: decodeIngestionJob,
    readSnapshot,
    handlers,
  });
  return null;
}
