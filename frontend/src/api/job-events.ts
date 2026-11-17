import {
  decodeBusinessError,
  decodeJobStatus,
  decodeMessage,
  decodeProgress,
  decodeTextDelta,
  isTerminal,
  type Decoder,
  type JobProgress,
  type JobStatusEvent,
  type PublishedMessageEvent,
  type PublishedTextDelta,
} from "./contracts";
import { apiPath, ApiError, errorMessage, responseError } from "./http";
import { readCursor, writeCursor } from "../storage/event-cursors";

export type JobKind = "ingestion" | "qa";
export type ConnectionState =
  "connecting" | "connected" | "recovering" | "closed" | "missing";
export interface EventHandlers<T extends JobStatusEvent> {
  snapshot(value: T): void;
  status(value: JobStatusEvent): void;
  progress(value: JobProgress, sequence: number): void;
  textDelta?(value: PublishedTextDelta): void;
  clarification?(value: PublishedMessageEvent): void;
  answer?(value: PublishedMessageEvent): void;
  reset(): void;
  connection(state: ConnectionState, message?: string): void;
  storageUnavailable(): void;
}

// Owns exactly one EventSource, one recovery request, and one retry timer per Job.
export function subscribeJobEvents<T extends JobStatusEvent>(options: {
  kind: JobKind;
  jobId: string;
  decodeSnapshot: Decoder<T>;
  readSnapshot(signal: AbortSignal): Promise<T>;
  handlers: EventHandlers<T>;
}): () => void {
  const { kind, jobId, handlers } = options;
  const path = `/${kind === "ingestion" ? "ingestion-jobs" : "qa-runs"}/${encodeURIComponent(jobId)}/events`;
  let cursor = readCursor(kind, jobId, handlers.storageUnavailable);
  let source: EventSource | undefined;
  let timer: ReturnType<typeof setTimeout> | undefined;
  let stopped = false;
  let recovering = false;
  let failures = 0;
  const controller = new AbortController();

  const closeSource = () => {
    source?.close();
    source = undefined;
  };
  const stop = (state: ConnectionState = "closed", message?: string) => {
    stopped = true;
    closeSource();
    clearTimeout(timer);
    handlers.connection(state, message);
  };
  const remember = (sequence: number) => {
    cursor = sequence;
    if (!writeCursor(kind, jobId, cursor)) handlers.storageUnavailable();
  };
  const schedule = () => {
    if (stopped) return;
    const delays = [1000, 2000, 5000, 10000];
    timer = setTimeout(connect, delays[Math.min(failures++, 3)]);
  };
  const recover = async (message?: string) => {
    if (stopped || recovering) return;
    recovering = true;
    closeSource();
    handlers.connection("recovering", message ?? "连接中断，正在恢复状态");
    try {
      const current = await options.readSnapshot(controller.signal);
      if (stopped) return;
      handlers.snapshot(current);
      if (isTerminal(current.status)) {
        stop();
        return;
      }
      // EventSource hides HTTP errors. Probe headers only; never consume a successful stream.
      const response = await fetch(apiPath(`${path}?after=${cursor}`), {
        signal: controller.signal,
        headers: { Accept: "text/event-stream" },
      });
      if (response.ok) {
        await response.body?.cancel();
      } else {
        const error = await responseError(response);
        if (error.business?.code === "SSE_CURSOR_INVALID") {
          remember(0);
          handlers.reset();
        } else if (error.status === 404) {
          stop("missing", error.message);
          return;
        } else handlers.connection("recovering", error.message);
      }
    } catch (error) {
      if (stopped) return;
      if (error instanceof ApiError && error.status === 404) {
        stop("missing", error.message);
        return;
      }
      handlers.connection("recovering", errorMessage(error));
    } finally {
      recovering = false;
    }
    schedule();
  };
  const finish = async () => {
    stop();
    try {
      const current = await options.readSnapshot(controller.signal);
      if (!controller.signal.aborted) handlers.snapshot(current);
    } catch (error) {
      if (!controller.signal.aborted)
        handlers.connection(
          "closed",
          `任务已结束，详情读取失败：${errorMessage(error)}`,
        );
    }
  };
  function connect() {
    if (stopped) return;
    handlers.connection("connecting");
    const current = new EventSource(apiPath(`${path}?after=${cursor}`));
    source = current;
    const listen = <P>(
      name: string,
      decode: Decoder<P>,
      consume: (payload: P, sequence: number) => void,
      persistent = true,
    ) => {
      current.addEventListener(name, (raw) => {
        if (stopped || source !== current) return;
        const event = raw as MessageEvent<string>;
        try {
          const sequence = persistent ? Number(event.lastEventId) : cursor;
          if (
            persistent &&
            (!/^\d+$/.test(event.lastEventId) ||
              !Number.isSafeInteger(sequence) ||
              sequence <= 0)
          )
            throw new Error("事件序号无效");
          if (persistent && sequence <= cursor) return;
          const payload = decode(JSON.parse(event.data));
          consume(payload, sequence);
          if (persistent) remember(sequence);
        } catch (error) {
          void recover(`事件读取失败：${errorMessage(error)}`);
        }
      });
    };
    listen("status", decodeJobStatus, (value) => {
      handlers.status(value);
      if (isTerminal(value.status)) void finish();
    });
    listen("progress", decodeProgress, handlers.progress);
    listen("text_delta", decodeTextDelta, (value) =>
      handlers.textDelta?.(value),
    );
    listen("clarification", decodeMessage, (value) =>
      handlers.clarification?.(value),
    );
    listen("answer", decodeMessage, (value) => handlers.answer?.(value));
    listen(
      "snapshot",
      options.decodeSnapshot,
      (value) => {
        handlers.snapshot(value);
        failures = 0;
        if (isTerminal(value.status)) stop();
        else handlers.connection("connected");
      },
      false,
    );
    listen(
      "stream_error",
      decodeBusinessError,
      (value) => {
        void recover(value.user_message);
      },
      false,
    );
    current.onerror = () => {
      void recover();
    };
  }
  connect();
  return () => {
    stopped = true;
    closeSource();
    clearTimeout(timer);
    controller.abort();
  };
}
