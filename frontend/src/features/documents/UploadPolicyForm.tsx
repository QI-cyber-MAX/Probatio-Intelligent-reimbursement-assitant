import { useRef, useState, type DragEvent, type FormEvent } from "react";
import type {
  DocumentItem,
  IngestionAccepted,
  KnowledgeBaseItem,
} from "../../api/contracts";
import { uploadPolicy } from "../../api/documents";
import { ApiError } from "../../api/http";
import { Dialog } from "../../ui/Dialog";
import { ErrorNotice } from "../../ui/ErrorNotice";
import { Field, FieldErrors } from "../../ui/Field";
import { Icon } from "../../ui/Icon";
import { PolicyRegistrationFields } from "./PolicyRegistrationFields";
import { emptyRegistration, policyFormData, resolveRegistration, validateRegistration, type PolicyRegistration } from "./policy-registration";

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
  const [draft, setDraft] = useState(() => emptyRegistration(candidates));
  const pending = useRef<{ file: File; registration: PolicyRegistration } | null>(null);
  const [file, setFile] = useState<File | null>(null);
  const [dragging, setDragging] = useState(false);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);
  const unknown = !!pending.current;
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
    if (!busy && !pending.current) pick(event.dataTransfer.files);
  }
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    const next = pending.current ? {} : validateRegistration(draft, candidates);
    if (!file) next.file = "请选择制度文件。";
    setErrors(next);
    if (Object.keys(next).length) return;
    const submission = pending.current ?? { file: file!, registration: resolveRegistration(draft, candidates) };
    const body = policyFormData(submission.file, submission.registration);
    setBusy(true);
    setError(null);
    try {
      onAccepted(await uploadPolicy(base.kb_id, body));
    } catch (failure) {
      if (failure instanceof ApiError && failure.outcomeUnknown) pending.current = submission;
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
        <PolicyRegistrationFields draft={draft} candidates={candidates} errors={errors} disabled={busy || !!pending.current} onChange={setDraft} />
        <fieldset disabled={busy || !!pending.current} className="form-fields">
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
