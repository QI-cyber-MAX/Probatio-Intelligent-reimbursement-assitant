import type { QuestionCreate } from "../api/contracts";
import { decodeWikiEdit, decodeMetadataUpdate, record } from "../api/contracts";

export interface PendingQuestion extends QuestionCreate {
  conversation_id: string;
}
const questionKey = (id: string) => `probatio:question:${id}`;
export function readPendingQuestion(
  id: string,
  unavailable?: () => void,
): PendingQuestion | null {
  try {
    const v: unknown = JSON.parse(
      sessionStorage.getItem(questionKey(id)) ?? "null",
    );
    if (
      !v ||
      typeof v !== "object" ||
      !("conversation_id" in v) ||
      v.conversation_id !== id ||
      !("message_id" in v) ||
      typeof v.message_id !== "string" ||
      !("kb_id" in v) ||
      typeof v.kb_id !== "string" ||
      !("question" in v) ||
      typeof v.question !== "string"
    )
      return null;
    return {
      conversation_id: id,
      message_id: v.message_id,
      kb_id: v.kb_id,
      question: v.question,
    };
  } catch {
    unavailable?.();
    return null;
  }
}

export interface PendingReply {
  message_id: string;
  job_id: string;
  prompt_message_id: string;
  text: string;
}
const replyKey = (id: string) => `probatio:reply:${id}`;
export function readPendingReply(
  id: string,
  unavailable?: () => void,
): PendingReply | null {
  try {
    const value: unknown = JSON.parse(
      sessionStorage.getItem(replyKey(id)) ?? "null",
    );
    if (
      !value ||
      typeof value !== "object" ||
      !("message_id" in value) ||
      typeof value.message_id !== "string" ||
      !("job_id" in value) ||
      typeof value.job_id !== "string" ||
      !("prompt_message_id" in value) ||
      typeof value.prompt_message_id !== "string" ||
      !("text" in value) ||
      typeof value.text !== "string"
    )
      return null;
    return {
      message_id: value.message_id,
      job_id: value.job_id,
      prompt_message_id: value.prompt_message_id,
      text: value.text,
    };
  } catch {
    unavailable?.();
    return null;
  }
}
export function savePendingReply(id: string, value: PendingReply): boolean {
  try {
    sessionStorage.setItem(replyKey(id), JSON.stringify(value));
    return true;
  } catch {
    return false;
  }
}
export function clearPendingReply(id: string): boolean {
  try {
    sessionStorage.removeItem(replyKey(id));
    return true;
  } catch {
    return false;
  }
}
export function savePendingQuestion(value: PendingQuestion): boolean {
  try {
    sessionStorage.setItem(
      questionKey(value.conversation_id),
      JSON.stringify(value),
    );
    return true;
  } catch {
    return false;
  }
}
export function clearPendingQuestion(id: string): boolean {
  try {
    sessionStorage.removeItem(questionKey(id));
    return true;
  } catch {
    return false;
  }
}

export interface PendingRebuild {
  old_job_id: string;
  new_job_id: string;
  document_id: string;
  source_hash: string;
}
const key = (documentId: string, sourceHash: string) =>
  `probatio:rebuild:${documentId}:${sourceHash}`;
export function readPendingRebuild(
  documentId: string,
  sourceHash: string,
  unavailable?: () => void,
): PendingRebuild | null {
  try {
    const raw = sessionStorage.getItem(key(documentId, sourceHash));
    if (!raw) return null;
    const v: unknown = JSON.parse(raw);
    if (
      !v ||
      typeof v !== "object" ||
      !("old_job_id" in v) ||
      !("new_job_id" in v) ||
      !("document_id" in v) ||
      !("source_hash" in v)
    )
      return null;
    if (
      typeof v.old_job_id !== "string" ||
      typeof v.new_job_id !== "string" ||
      v.document_id !== documentId ||
      v.source_hash !== sourceHash
    )
      return null;
    return {
      old_job_id: v.old_job_id,
      new_job_id: v.new_job_id,
      document_id: documentId,
      source_hash: sourceHash,
    };
  } catch {
    unavailable?.();
    return null;
  }
}
export function savePendingRebuild(value: PendingRebuild): boolean {
  try {
    sessionStorage.setItem(
      key(value.document_id, value.source_hash),
      JSON.stringify(value),
    );
    return true;
  } catch {
    return false;
  }
}
export function clearPendingRebuild(
  documentId: string,
  sourceHash: string,
): boolean {
  try {
    sessionStorage.removeItem(key(documentId, sourceHash));
    return true;
  } catch {
    return false;
  }
}

export interface WikiDraft {
  request: import("../api/contracts").WikiEditCreate;
  submitted: boolean;
}
const wikiKey = (kb: string) => `probatio:wiki:${kb}`;
export function readWikiDraft(
  kb: string,
  unavailable?: () => void,
): WikiDraft | null {
  try {
    const raw = sessionStorage.getItem(wikiKey(kb));
    if (!raw) return null;
    const value = JSON.parse(raw) as unknown;
    const v = record(value);
    if (typeof v.submitted !== "boolean")
      throw new Error("Invalid draft state");
    return { request: decodeWikiEdit(v.request), submitted: v.submitted };
  } catch {
    unavailable?.();
    return null;
  }
}
export function saveWikiDraft(kb: string, value: WikiDraft): boolean {
  try {
    sessionStorage.setItem(wikiKey(kb), JSON.stringify(value));
    return true;
  } catch {
    return false;
  }
}
export function clearWikiDraft(kb: string): boolean {
  try {
    sessionStorage.removeItem(wikiKey(kb));
    return true;
  } catch {
    return false;
  }
}

const metadataKey = (documentId: string, hash: string) => `probatio:metadata:${documentId}:${hash}`;
export function readPendingMetadata(documentId: string, hash: string, unavailable: () => void) {
  try {
    const raw = sessionStorage.getItem(metadataKey(documentId, hash));
    return raw ? decodeMetadataUpdate(JSON.parse(raw)) : null;
  } catch { unavailable(); return null; }
}
export function savePendingMetadata(documentId: string, hash: string, value: import("../api/contracts").SourceMetadataUpdate | null) {
  try {
    if (value) sessionStorage.setItem(metadataKey(documentId, hash), JSON.stringify(value));
    else sessionStorage.removeItem(metadataKey(documentId, hash));
    return true;
  } catch { return false; }
}
