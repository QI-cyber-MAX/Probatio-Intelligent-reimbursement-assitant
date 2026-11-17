import { useRef, useState, type FormEvent } from "react";
import type { KnowledgeBase } from "../api/contracts";
import { createKnowledgeBase } from "../api/documents";
import { ApiError } from "../api/http";
import { Dialog } from "../ui/Dialog";
import { Field } from "../ui/Field";
import { ErrorNotice } from "../ui/ErrorNotice";

export function CreateKnowledgeBaseDialog({
  onClose,
  onCreated,
  onRefresh,
}: {
  onClose(): void;
  onCreated(base: KnowledgeBase): void;
  onRefresh(): void;
}) {
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [fieldError, setFieldError] = useState("");
  const input = useRef<HTMLInputElement>(null);
  const unknown = error instanceof ApiError && error.outcomeUnknown;
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (busy || unknown) return;
    const value = name.trim();
    if (!value || Array.from(value).length > 200) {
      setFieldError("名称须为 1—200 个字符。");
      input.current?.focus();
      return;
    }
    setBusy(true);
    setError(null);
    setFieldError("");
    try {
      onCreated(await createKnowledgeBase(value));
    } catch (failure) {
      setError(failure);
      if (failure instanceof ApiError && failure.fields.name) {
        setFieldError(failure.fields.name);
        input.current?.focus();
      }
    } finally {
      setBusy(false);
    }
  }
  return (
    <Dialog title="创建知识库" onClose={onClose} busy={busy}>
      <form onSubmit={submit} noValidate>
        <p className="muted form-intro">将同一业务范围的制度收纳在一起。</p>
        <Field id="kb-name" label="知识库名称（必填）" error={fieldError}>
          <input
            ref={input}
            id="kb-name"
            autoFocus
            value={name}
            onChange={(event) => setName(event.target.value)}
            aria-invalid={!!fieldError}
            aria-describedby={fieldError ? "kb-name-error" : undefined}
            readOnly={busy || unknown}
          />
        </Field>
        {error !== null && <ErrorNotice error={error} />}
        {unknown && (
          <p className="help">
            创建结果未确认，请刷新列表辨识新记录，避免重复创建。
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
          {unknown ? (
            <button
              type="button"
              className="button primary"
              onClick={() => {
                onRefresh();
                onClose();
              }}
            >
              刷新知识库列表
            </button>
          ) : (
            <button className="button primary" disabled={busy}>
              {busy ? "正在创建…" : "创建并选中"}
            </button>
          )}
        </footer>
      </form>
    </Dialog>
  );
}
