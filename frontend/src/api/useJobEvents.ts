import { useEffect } from "react";
import { type JobStatusEvent } from "./contracts";
import { subscribeJobEvents } from "./job-events";

export function useJobEvents<T extends JobStatusEvent>(
  options: Omit<Parameters<typeof subscribeJobEvents<T>>[0], "jobId"> & {
    jobId: string | null;
  },
): void {
  const { kind, jobId, decodeSnapshot, readSnapshot, handlers } = options;
  useEffect(
    () =>
      jobId
        ? subscribeJobEvents({
            kind,
            jobId,
            decodeSnapshot,
            readSnapshot,
            handlers,
          })
        : undefined,
    [kind, jobId, decodeSnapshot, readSnapshot, handlers],
  );
}
