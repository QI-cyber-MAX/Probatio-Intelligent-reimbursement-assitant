import { useRef, useState, type DragEvent, type FormEvent } from "react";
import type {
  DocumentItem,
  IngestionAccepted,
  KnowledgeBaseItem,
  VersionStatus,
} from "../../api/contracts";
import { uploadPolicy } from "../../api/documents";
import { ApiError } from "../../api/http";
import { Dialog } from "../../ui/Dialog";
import { ErrorNotice } from "../../ui/ErrorNotice";
import { Field, FieldErrors } from "../../ui/Field";
import { Icon } from "../../ui/Icon";

export function UploadPolicyForm({
  base,
  candidates,
  onClose,
  onAccepted,
  onReload,
}: {
  base: KnowledgeBaseItem;
  candidates: DocumentItem[];
  onClose(): void;
  onAccepted(value: IngestionAccepted): void;
  onReload(): void;
}) {
  const [mode, setMode] = useState<"new" | "existing">("new");
  const [documentId, setDocumentId] = useState(
    candidates[0]?.document_id ?? "",
  );
  const [title, setTitle] = useState("");
  const [issuer, setIssuer] = useState("");
  const [version, setVersion] = useState<VersionStatus | "">("");
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [dragging, setDragging] = useState(false);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);
  const existing = candidates.find(
    (candidate) => candidate.document_id === documentId,
  );
  const actualIssuer = mode === "existing" ? (existing?.issuer ?? "") : issuer;
  const unknown = error instanceof ApiError && error.outcomeUnknown;
  function pick(files: FileList | null) {
    if (!files?.length) return;
    if (files.length !== 1) {
      setErrors({ file: "每次请选择一个文件。" });
      return;
    }
    setFile(files[0]!);
    setErrors((previous) => {
      const next = { ...previous };
      delete next.file;
      return next;
    });
  }
  function drop(event: DragEvent) {
    event.preventDefault();
    setDragging(false);
    if (!busy) pick(event.dataTransfer.files);
  }
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    const next: Record<string, string> = {};
    if (mode === "new" && !title.trim()) next.title = "请填写制度名。";
    if (mode === "existing" && !existing) next.document_id = "请选择已有制度。";
    if (!actualIssuer.trim()) next.issuer = "请填写发文单位。";
    if (mode === "existing" && !version) next.version_status = "请选择历史版本或新版本。";
    if (from && to && to <= from)
      next.effective_to = "截止日期须晚于生效日期。";
    if (!file) next.file = "请选择制度文件。";
    setErrors(next);
    if (Object.keys(next).length) return;
    const body = new FormData();
    body.append("file", file!);
    body.append("issuer", actualIssuer.trim());
    body.append("version_status", mode === "new" ? "CURRENT" : version);
    if (from) body.append("effective_from", from);
    if (to) body.append("effective_to", to);
    if (mode === "new") body.append("title", title.trim());
    else body.append("document_id", documentId);
    setBusy(true);
    setError(null);
    try {
      onAccepted(await uploadPolicy(base.kb_id, body));
    } catch (failure) {
      setError(failure);
      if (failure instanceof ApiError) {
        const fields = Object.fromEntries(
          Object.entries(failure.fields).filter(([key]) =>
            [
              "version_status",
              "title",
              "document_id",
              "issuer",
              "effective_from",
              "effective_to",
              "file",
            ].includes(key),
          ),
        );
        setErrors(fields);
      }
    } finally {
      setBusy(false);
    }
  }
  const described = (id: string, hint = false) =>
    [hint ? `${id}-hint` : "", errors[id] ? `${id}-error` : ""]
      .filter(Boolean)
      .join(" ") || undefined;
  return (
    <Dialog title="上传制度" drawer busy={busy} onClose={onClose}>
      <form onSubmit={submit} noValidate aria-busy={busy}>
        <p className="muted form-intro">登记适用日期，上传制度原件。</p>
        <FieldErrors errors={errors} />
        <Field id="upload-kb" label="目标知识库">
          <input id="upload-kb" value={base.name} readOnly />
        </Field>
        <fieldset disabled={busy} className="mode-switch">
          <legend>制度登记方式</legend>
          <label>
            <input
              type="radio"
              name="mode"
              value="new"
              checked={mode === "new"}
              onChange={() => setMode("new")}
            />
            新制度
          </label>
          <label>
            <input
              type="radio"
              name="mode"
              value="existing"
              checked={mode === "existing"}
              disabled={!candidates.length}
              onChange={() => setMode("existing")}
            />
            已有制度
          </label>
        </fieldset>
        <fieldset disabled={busy} className="form-fields">
          {mode === "new" ? (
            <Field id="title" label="制度名（必填）" error={errors.title}>
              <input
                id="title"
                value={title}
                onChange={(event) => setTitle(event.target.value)}
                placeholder="填写规范制度名，而非文件名"
                aria-invalid={!!errors.title}
                aria-describedby={described("title")}
              />
            </Field>
          ) : (
            <Field
              id="document_id"
              label="已有制度（必填）"
              error={errors.document_id}
            >
              <select
                id="document_id"
                value={documentId}
                onChange={(event) => setDocumentId(event.target.value)}
                aria-invalid={!!errors.document_id}
                aria-describedby={described("document_id")}
              >
                {candidates.map((candidate) => (
                  <option
                    key={candidate.document_id}
                    value={candidate.document_id}
                  >
                    {candidate.title} · {candidate.issuer}
                  </option>
                ))}
              </select>
            </Field>
          )}
          <Field id="issuer" label="发文单位（必填）" error={errors.issuer}>
            <input
              id="issuer"
              value={actualIssuer}
              readOnly={mode === "existing"}
              onChange={(event) => setIssuer(event.target.value)}
              aria-invalid={!!errors.issuer}
              aria-describedby={described("issuer")}
            />
          </Field>
          {mode === "existing" && (
            <Field id="version_status" label="原件版本（必填）" error={errors.version_status}>
              <select id="version_status" value={version} aria-invalid={!!errors.version_status} aria-describedby={described("version_status")}
                onChange={(event) => setVersion(event.target.value as VersionStatus | "")}>
                <option value="">请选择版本</option>
                <option value="CURRENT">新版本</option>
                <option value="HISTORICAL">历史版本</option>
              </select>
            </Field>
          )}
          <div className="date-fields">
            <Field
              id="effective_from"
              label="生效日期（选填）"
              hint="留空时按原文提取生效日期"
              error={errors.effective_from}
            >
              <input
                type="date"
                id="effective_from"
                value={from}
                onChange={(event) => setFrom(event.target.value)}
                aria-invalid={!!errors.effective_from}
                aria-describedby={described("effective_from", true)}
              />
            </Field>
            <Field
              id="effective_to"
              label="截止日期（选填）"
              hint="从该日起不再适用"
              error={errors.effective_to}
            >
              <input
                type="date"
                id="effective_to"
                value={to}
                onChange={(event) => setTo(event.target.value)}
                aria-invalid={!!errors.effective_to}
                aria-describedby={described("effective_to", true)}
              />
            </Field>
          </div>
          <Field
            id="file"
            label="制度文件（必填）"
            error={errors.file}
            hint="PDF、DOCX、XLSX、DOC · 默认上限 50 MiB，实际以服务端为准"
          >
            <div
              className={`file-drop ${dragging ? "dragging" : ""}`}
              onDragOver={(event) => {
                event.preventDefault();
                setDragging(true);
              }}
              onDragLeave={() => setDragging(false)}
              onDrop={drop}
            >
              <Icon name={file ? "document" : "upload"} />
              <p>{file ? file.name : "拖放制度文件到此处"}</p>
              {file && (
                <p className="help">{(file.size / 1024).toFixed(1)} KiB</p>
              )}
              <input
                id="file"
                type="file"
                ref={fileInput}
                className="sr-only"
                accept=".pdf,.docx,.xlsx,.doc"
                onChange={(event) => pick(event.target.files)}
                aria-invalid={!!errors.file}
                aria-describedby={described("file", true)}
              />
              <div className="file-actions">
                <button
                  type="button"
                  className="button secondary"
                  onClick={() => fileInput.current?.click()}
                >
                  {file ? "替换文件" : "选择文件"}
                </button>
                {file && (
                  <button
                    type="button"
                    className="text-button"
                    onClick={() => {
                      setFile(null);
                      if (fileInput.current) fileInput.current.value = "";
                    }}
                  >
                    移除
                  </button>
                )}
              </div>
            </div>
          </Field>
        </fieldset>
        {error !== null && <ErrorNotice error={error} />}
        {unknown && (
          <div className="notice warning">
            <p>
              上传结果未确认。先重新读取原件列表；确认后可用相同文件与登记信息重传。
            </p>
            <button type="button" className="text-button" onClick={onReload}>
              核对原件列表
            </button>
          </div>
        )}
        {busy && (
          <p className="notice" role="status">
            <span className="spinner" />
            正在上传并检查文件
          </p>
        )}
        <footer className="form-actions">
          <button
            type="button"
            className="button secondary"
            disabled={busy}
            onClick={onClose}
          >
            关闭
          </button>
          <button type="submit" className="button primary" disabled={busy}>
            <Icon name="upload" />
            {busy
              ? "正在上传并检查文件"
              : unknown
                ? "重传相同登记"
                : "上传并处理"}
          </button>
        </footer>
      </form>
    </Dialog>
  );
}
