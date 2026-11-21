import { useMemo, useRef, useState } from "react";
import type { DocumentItem, IngestionAccepted, KnowledgeBaseItem } from "../../api/contracts";
import { Dialog } from "../../ui/Dialog";
import { ErrorNotice } from "../../ui/ErrorNotice";
import { Field, FieldErrors } from "../../ui/Field";
import { Icon } from "../../ui/Icon";
import { StatusBadge } from "../../ui/StatusBadge";
import { PolicyRegistrationFields } from "./PolicyRegistrationFields";
import { stageLabels, statusLabels } from "./job-labels";
import type { Tracking } from "./useIngestionJobs";
import type { usePolicyUploadQueue } from "./usePolicyUploadQueue";

const labels = { queued: "排队", uploading: "上传与检查", accepted: "已受理", failed: "受理失败", unknown: "结果未确认" };
export function BatchUploadPolicyForm({ base, candidates, queue, tracking, receiptErrors, onClose, onReload, onRead, onView }: {
  base: KnowledgeBaseItem;
  candidates: DocumentItem[];
  queue: ReturnType<typeof usePolicyUploadQueue>;
  tracking: Record<string, Tracking>;
  receiptErrors: Record<string, unknown>;
  onClose(): void;
  onReload(): void;
  onRead(value: IngestionAccepted): void;
  onView(value: IngestionAccepted): void;
}) {
  const input = useRef<HTMLInputElement>(null);
  const [issuer, setIssuer] = useState("");
  const [dragging, setDragging] = useState(false);
  const errors = useMemo(() => Object.fromEntries(queue.rows.flatMap((row) =>
    Object.entries(row.errors).map(([key, value]) => [`${row.id}-${key}`, `${row.file.name}：${value}`]),
  )), [queue.rows]);
  return <Dialog title="批量上传制度" drawer busy={queue.running} onClose={onClose}>
    <form noValidate onSubmit={(event) => { event.preventDefault(); void queue.start(); }} aria-busy={queue.running}>
      <p className="muted form-intro">逐份确认制度归属、版本和日期，按顺序上传到「{base.name}」。</p>
      <FieldErrors errors={errors} />
      <fieldset disabled={queue.running}>
        <Field id="batch-files" label="选择制度文件" hint="支持多选或拖放 PDF、DOCX、XLSX、DOC；每份默认上限 50 MiB，实际以服务端为准。">
          <div className={`file-drop ${dragging ? "dragging" : ""}`} onDragOver={(event) => { event.preventDefault(); if (!queue.running) setDragging(true); }}
            onDragLeave={() => setDragging(false)} onDrop={(event) => { event.preventDefault(); setDragging(false); if (!queue.running) queue.add(Array.from(event.dataTransfer.files)); }}>
            <Icon name="upload" /><p>拖放多份制度文件到此处</p>
            <input id="batch-files" className="sr-only" type="file" multiple accept=".pdf,.docx,.xlsx,.doc" ref={input}
              aria-describedby="batch-files-hint" onChange={(event) => { queue.add(Array.from(event.target.files ?? [])); event.target.value = ""; }} />
            <button type="button" className="button secondary" onClick={() => input.current?.click()}>选择多个文件</button>
          </div>
        </Field>
        <Field id="batch-issuer" label="批量填写发文单位" hint="应用到选中且可编辑的新制度文件。">
          <input id="batch-issuer" value={issuer} onChange={(event) => setIssuer(event.target.value)} aria-describedby="batch-issuer-hint" />
        </Field>
        <button type="button" className="button secondary" disabled={!issuer.trim()} onClick={() => queue.applyIssuer(issuer)}>将发文单位应用到选中项</button>
      </fieldset>
      <p className="notice batch-summary" role="status" aria-atomic="true">
        总数 {queue.rows.length} · 已受理 {queue.rows.filter((row) => row.state === "accepted").length} · 受理失败 {queue.rows.filter((row) => row.state === "failed").length} · 结果未确认 {queue.rows.filter((row) => row.state === "unknown").length} · 排队 {queue.rows.filter((row) => row.state === "queued").length}
      </p>
      <div className="batch-rows">
        {queue.rows.map((row) => {
          const editable = !queue.running && (row.state === "queued" || row.state === "failed");
          const submitted = !editable ? row.submission?.registration : undefined;
          const draft = submitted ? { mode: submitted.documentId ? "existing" as const : "new" as const,
            documentId: submitted.documentId ?? "", title: submitted.title, issuer: submitted.issuer,
            version: submitted.version, from: submitted.from, to: submitted.to } : row.draft;
          const options = submitted?.documentId ? [...candidates.filter((item) => item.document_id !== submitted.documentId),
            { document_id: submitted.documentId, title: submitted.title, issuer: submitted.issuer }] : candidates;
          const job = row.accepted ? tracking[row.accepted.job_id] : undefined;
          const status = job?.status ?? job?.view;
          return <section className="batch-row" key={row.id} aria-labelledby={`${row.id}-name`}>
            <header className="batch-row-heading">
              <h3 id={`${row.id}-name`}>{row.file.name}</h3><StatusBadge tone={row.state === "failed" ? "danger" : row.state === "unknown" ? "warning" : row.state === "accepted" ? "success" : "neutral"}>{labels[row.state]}</StatusBadge>
            </header>
            <p className="help">{(row.file.size / 1024).toFixed(1)} KiB</p>
            {(row.state === "queued" || row.state === "failed") && <div className="batch-selection">
              <label><input type="checkbox" checked={row.selected} disabled={queue.running} onChange={(event) => queue.select(row.id, event.target.checked)} />选中此文件</label>
              <button type="button" className="text-button" disabled={!editable} onClick={() => queue.remove(row.id)}>移除文件</button>
            </div>}
            <PolicyRegistrationFields id={row.id} draft={draft} candidates={options} errors={row.errors} disabled={!editable} batch onChange={(value) => queue.edit(row.id, value)} />
            {row.error !== undefined && <ErrorNotice error={row.error} />}
            {row.state === "queued" && row.draft.version === "HISTORICAL" && <p className="help">首次登记同一制度时，取得新版本回执后上传此历史文件。</p>}
            {row.state === "failed" && <button type="button" className="button secondary" disabled={queue.running} onClick={() => void queue.start(row.id)}>重试此文件</button>}
            {row.state === "unknown" && <div className="notice warning">
              <p>上传结果未确认，保留本次文件与登记。取得准确回执后继续跟踪。</p>
              <div className="file-actions">
                <button type="button" className="text-button" onClick={onReload}>核对原件列表</button>
                <button type="button" className="button secondary" disabled={queue.running} onClick={() => void queue.start(row.id)}>重传相同登记</button>
              </div>
            </div>}
            {row.accepted && <div className="notice">
              <p>原件已受理 · 任务 <code>{row.accepted.job_id.slice(0, 8)}</code></p>
              <p>{status ? `${statusLabels[status.status]}${job?.view && !["SUCCEEDED", "FAILED", "CANCELLED"].includes(status.status) ? ` · ${stageLabels[job.view.stage]}` : ""}` : "正在读取处理状态"}</p>
              {status?.terminal_reason && <p>{status.terminal_reason}</p>}
              {receiptErrors[row.accepted.job_id] !== undefined && <ErrorNotice error={receiptErrors[row.accepted.job_id]} retry={() => onRead(row.accepted!)} />}
              <button type="button" className="text-button" disabled={queue.running} onClick={() => onView(row.accepted!)}>查看原件与任务操作</button>
            </div>}
          </section>;
        })}
      </div>
      <footer className="form-actions">
        <button type="button" className="button secondary" disabled={queue.running} onClick={onClose}>关闭</button>
        {queue.running ? <button type="button" className="button secondary" disabled={queue.pausing} onClick={queue.pause}>{queue.pausing ? "当前请求完成后暂停" : "暂停上传"}</button>
          : <button type="submit" className="button primary" disabled={!queue.canStart}>{queue.started ? "继续上传" : "开始上传"}</button>}
      </footer>
    </form>
  </Dialog>;
}
