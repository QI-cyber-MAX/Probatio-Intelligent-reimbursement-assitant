import type { ExecutionStatus } from "../../api/contracts";
export const qaStatusLabels: Record<ExecutionStatus, string> = {
  ACCEPTED: "已登记",
  QUEUED: "排队中",
  RUNNING: "处理中",
  RETRY_WAIT: "等待自动重试",
  WAITING_INPUT: "等待补充",
  CANCELLING: "正在取消",
  SUCCEEDED: "已完成",
  FAILED: "处理失败",
  CANCELLED: "已取消",
};
