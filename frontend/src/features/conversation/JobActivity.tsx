import { isTerminal, type QAJobView } from "../../api/contracts";
import { StatusBadge } from "../../ui/StatusBadge";
import type { ConnectionState } from "../../api/job-events";
import type { ActionEntry } from "./useConversation";
import { qaStatusLabels } from "./job-labels";
export function JobActivity({
  job,
  actions,
  connection,
  cancelling,
  onCancel,
  clarificationVisible = false,
}: {
  job: QAJobView;
  actions: ActionEntry[];
  connection?: { state: ConnectionState; message?: string };
  cancelling: boolean;
  onCancel(): void;
  clarificationVisible?: boolean;
}) {
  const terminal = isTerminal(job.status);
  return (
    <div className="job-activity">
      <div className="job-activity-heading">
        <StatusBadge
          tone={
            job.status === "SUCCEEDED"
              ? "success"
              : job.status === "FAILED"
                ? "danger"
                : job.status === "WAITING_INPUT" || job.status === "RETRY_WAIT"
                  ? "warning"
                  : "neutral"
          }
        >
          {qaStatusLabels[job.status]}
        </StatusBadge>
        <details className="job-details">
          <summary>
            任务详情{actions.length > 0 && ` · ${actions.length} 条处理记录`}
          </summary>
          <code>{job.job_id}</code>
          {job.last_error_code && (
            <p className="help">错误代码：{job.last_error_code}</p>
          )}
          {actions.length > 0 && (
            <ol className="action-log">
              {actions.map((action) => (
                <li key={action.sequence}>
                  <p>{action.text}</p>
                  {(action.item_id || action.tool_call_id) && (
                    <p className="help">
                      {action.item_id && `问题项 ${action.item_id}`}
                      {action.tool_call_id && ` · 工具 ${action.tool_call_id}`}
                    </p>
                  )}
                </li>
              ))}
            </ol>
          )}
        </details>
        {!terminal && !clarificationVisible && (
          <button
            className="text-button danger-text"
            disabled={cancelling || job.status === "CANCELLING"}
            onClick={onCancel}
          >
            取消任务
          </button>
        )}
      </div>
      {job.terminal_reason && <p className="help">{job.terminal_reason}</p>}
      {job.status === "RETRY_WAIT" && (
        <p className="help">
          自动重试 {job.retry_count} / {job.max_retries}
          {job.next_retry_at &&
            ` · ${new Date(job.next_retry_at).toLocaleString("zh-CN")} 后重试`}
        </p>
      )}
      {!terminal && connection?.state === "recovering" && (
        <p className="help">
          连接中断，正在恢复状态
          {connection.message && `：${connection.message}`}
        </p>
      )}
      {!terminal && connection?.state === "connecting" && <p className="help">正在连接处理记录…</p>}
      {connection?.state === "missing" && (
        <p className="danger-text">{connection.message}</p>
      )}
      {!terminal && job.status !== "WAITING_INPUT" && (
        <p className="latest-action">
          <span className="spinner" aria-hidden="true" />
          {actions.at(-1)?.text ?? "已恢复任务，等待新的处理记录。"}
        </p>
      )}
    </div>
  );
}
