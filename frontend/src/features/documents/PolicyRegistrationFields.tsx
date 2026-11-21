import type { DocumentItem, VersionStatus } from "../../api/contracts";
import { Field } from "../../ui/Field";
import type { PolicyRegistrationDraft } from "./policy-registration";

export function PolicyRegistrationFields({ id = "", draft, candidates, errors, disabled, batch = false, onChange }: {
  id?: string;
  draft: PolicyRegistrationDraft;
  candidates: DocumentItem[];
  errors: Record<string, string>;
  disabled: boolean;
  batch?: boolean;
  onChange(value: PolicyRegistrationDraft): void;
}) {
  const fieldId = (key: string) => id ? `${id}-${key}` : key;
  const described = (key: string, hint = false) => [hint ? `${fieldId(key)}-hint` : "", errors[key] ? `${fieldId(key)}-error` : ""].filter(Boolean).join(" ") || undefined;
  const change = (patch: Partial<PolicyRegistrationDraft>) => onChange({ ...draft, ...patch });
  const existing = candidates.find((item) => item.document_id === draft.documentId);
  return <>
    <fieldset disabled={disabled} className="mode-switch">
      <legend>制度登记方式</legend>
      {(["new", "existing"] as const).map((mode) => <label key={mode}>
        <input type="radio" name={fieldId("mode")} checked={draft.mode === mode} disabled={mode === "existing" && !candidates.length}
          onChange={() => change({ mode, documentId: draft.documentId || candidates[0]?.document_id || "" })} />
        {mode === "new" ? "新制度" : "已有制度"}
      </label>)}
    </fieldset>
    <fieldset disabled={disabled} className="form-fields">
      {draft.mode === "new" ? <Field id={fieldId("title")} label="制度名（必填）" error={errors.title}>
        <input id={fieldId("title")} value={draft.title} onChange={(event) => change({ title: event.target.value })}
          placeholder="填写规范制度名，而非文件名" aria-invalid={!!errors.title} aria-describedby={described("title")} />
      </Field> : <Field id={fieldId("document_id")} label="已有制度（必填）" error={errors.document_id}>
        <select id={fieldId("document_id")} value={draft.documentId} onChange={(event) => change({ documentId: event.target.value })}
          aria-invalid={!!errors.document_id} aria-describedby={described("document_id")}>
          <option value="">请选择制度</option>
          {candidates.map((item) => <option key={item.document_id} value={item.document_id}>{item.title} · {item.issuer}</option>)}
        </select>
      </Field>}
      <Field id={fieldId("issuer")} label="发文单位（必填）" error={errors.issuer}>
        <input id={fieldId("issuer")} value={draft.mode === "existing" ? existing?.issuer ?? "" : draft.issuer} readOnly={draft.mode === "existing"}
          onChange={(event) => change({ issuer: event.target.value })} aria-invalid={!!errors.issuer} aria-describedby={described("issuer")} />
      </Field>
      {(batch || draft.mode === "existing") && <Field id={fieldId("version_status")} label="原件版本（必填）" error={errors.version_status}>
        <select id={fieldId("version_status")} value={draft.version} aria-invalid={!!errors.version_status} aria-describedby={described("version_status")}
          onChange={(event) => change({ version: event.target.value as VersionStatus | "" })}>
          <option value="">请选择版本</option><option value="CURRENT">新版本</option><option value="HISTORICAL">历史版本</option>
        </select>
      </Field>}
      <div className="date-fields">
        <Field id={fieldId("effective_from")} label="生效日期（选填）" hint="留空时按原文提取生效日期" error={errors.effective_from}>
          <input type="date" id={fieldId("effective_from")} value={draft.from} onChange={(event) => change({ from: event.target.value })}
            aria-invalid={!!errors.effective_from} aria-describedby={described("effective_from", true)} />
        </Field>
        <Field id={fieldId("effective_to")} label="截止日期（选填）" hint="从该日起不再适用" error={errors.effective_to}>
          <input type="date" id={fieldId("effective_to")} value={draft.to} onChange={(event) => change({ to: event.target.value })}
            aria-invalid={!!errors.effective_to} aria-describedby={described("effective_to", true)} />
        </Field>
      </div>
    </fieldset>
  </>;
}
