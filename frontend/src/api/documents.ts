import {
  array,
  decodeAccepted,
  decodeDocument,
  decodeDocumentItem,
  decodeIngestionJob,
  decodeKnowledgeBase,
  decodeKnowledgeBaseItem,
  type IngestionAccepted,
  type SourceMetadataUpdate,
} from "./contracts";
import { request } from "./http";

const part = encodeURIComponent;
export const listKnowledgeBases = (signal?: AbortSignal) =>
  request("/knowledge-bases", array(decodeKnowledgeBaseItem), { signal });
export const createKnowledgeBase = (name: string) =>
  request("/knowledge-bases", decodeKnowledgeBase, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  });
export const listDocuments = (kb: string, signal?: AbortSignal) =>
  request(`/knowledge-bases/${part(kb)}/documents`, array(decodeDocumentItem), {
    signal,
  });
export const getDocument = (id: string, signal?: AbortSignal) =>
  request(`/documents/${part(id)}`, decodeDocument, { signal });
export const uploadPolicy = (kb: string, body: FormData) =>
  request(`/knowledge-bases/${part(kb)}/documents`, decodeAccepted, {
    method: "POST",
    body,
  });
export const getIngestionJob = (id: string, signal?: AbortSignal) =>
  request(`/ingestion-jobs/${part(id)}`, decodeIngestionJob, { signal });
export const getAcceptedJob = (
  accepted: IngestionAccepted,
  signal?: AbortSignal,
) => request(accepted.status_url, decodeIngestionJob, { signal });
export const cancelIngestion = (id: string, signal?: AbortSignal) =>
  request(`/ingestion-jobs/${part(id)}/cancel`, decodeIngestionJob, {
    method: "POST",
    signal,
  });
export const rebuildIngestion = (id: string, newJobId: string, signal?: AbortSignal) =>
  request(`/ingestion-jobs/${part(id)}/retry`, decodeAccepted, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ new_job_id: newJobId }),
    signal,
  });

export const updateSourceMetadata = (documentId: string, sourceHash: string, body: SourceMetadataUpdate) =>
  request(`/documents/${part(documentId)}/sources/${part(sourceHash)}/metadata`, decodeAccepted, {
    method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
