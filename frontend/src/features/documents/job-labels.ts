import type { ExecutionStatus, IngestionStage } from "../../api/contracts";
export const statusLabels: Record<ExecutionStatus, string> = {
  ACCEPTED: "已登记",
  QUEUED: "排队中",
  RUNNING: "处理中",
  RETRY_WAIT: "等待自动重试",
  WAITING_INPUT: "等待补充",
  CANCELLING: "正在取消",
  SUCCEEDED: "已发布",
  FAILED: "处理失败",
  CANCELLED: "已取消",
};

export const stageLabels: Record<IngestionStage, string> = {
  REGISTERED: "已登记",
  PARSING: "解析文档",
  NORMALIZING: "归一化结构",
  CHUNKING: "分块",
  EMBEDDING: "生成向量",
  INDEXING: "写入索引",
  PUBLISHING: "发布",
};
