import type { DocumentItem, VersionStatus } from "../../api/contracts";

export interface PolicyRegistrationDraft {
  mode: "new" | "existing";
  documentId: string;
  title: string;
  issuer: string;
  version: VersionStatus | "";
  from: string;
  to: string;
}
export interface PolicyRegistration {
  documentId?: string;
  title: string;
  issuer: string;
  version: VersionStatus;
  from: string;
  to: string;
}
export const emptyRegistration = (
  candidates: DocumentItem[],
): PolicyRegistrationDraft => ({
  mode: "new",
  documentId: candidates[0]?.document_id ?? "",
  title: "",
  issuer: "",
  version: "",
  from: "",
  to: "",
});
export function resolveRegistration(
  draft: PolicyRegistrationDraft,
  candidates: DocumentItem[],
  batch = false,
): PolicyRegistration {
  const existing = candidates.find((item) => item.document_id === draft.documentId);
  return {
    documentId: draft.mode === "existing" ? existing?.document_id : undefined,
    title: (draft.mode === "existing" ? existing?.title ?? "" : draft.title).trim(),
    issuer: (draft.mode === "existing" ? existing?.issuer ?? "" : draft.issuer).trim(),
    version: draft.mode === "new" && !batch ? "CURRENT" : draft.version as VersionStatus,
    from: draft.from,
    to: draft.to,
  };
}
export function validateRegistration(
  draft: PolicyRegistrationDraft,
  candidates: DocumentItem[],
  batch = false,
): Record<string, string> {
  const value = resolveRegistration(draft, candidates, batch);
  const errors: Record<string, string> = {};
  if (draft.mode === "new" && !value.title) errors.title = "请填写制度名。";
  if (draft.mode === "existing" && !value.documentId)
    errors.document_id = "请选择已有制度。";
  if (!value.issuer) errors.issuer = "请填写发文单位。";
  if ((batch || draft.mode === "existing") && !draft.version)
    errors.version_status = "请选择历史版本或新版本。";
  if (value.from && value.to && value.to <= value.from)
    errors.effective_to = "截止日期须晚于生效日期。";
  return errors;
}
export const registrationIdentity = (kb: string, value: PolicyRegistration) =>
  JSON.stringify([kb, value.issuer, value.title]);

export function policyFormData(file: File, value: PolicyRegistration): FormData {
  const body = new FormData();
  body.append("file", file);
  body.append("issuer", value.issuer);
  body.append("version_status", value.version);
  if (value.from) body.append("effective_from", value.from);
  if (value.to) body.append("effective_to", value.to);
  if (value.documentId) body.append("document_id", value.documentId);
  else body.append("title", value.title);
  return body;
}
