import {
  decodeQAAccepted,
  decodeWikiWorkspace,
  decodeWikiJob,
  decodeWikiChunks,
  type WikiEditCreate,
} from "./contracts";
import { request } from "./http";

export const getWiki = (kb: string, signal?: AbortSignal) =>
  request(
    `/knowledge-bases/${encodeURIComponent(kb)}/wiki`,
    decodeWikiWorkspace,
    { signal },
  );
export const getWikiJob = (job: string, signal?: AbortSignal) =>
  request(`/wiki-jobs/${encodeURIComponent(job)}`, decodeWikiJob, { signal });
export const getWikiChunks = (
  kb: string,
  page: string,
  version: string,
  signal?: AbortSignal,
) =>
  request(
    `/knowledge-bases/${encodeURIComponent(kb)}/wiki/source-pages/${encodeURIComponent(page)}/chunks?version_job_id=${encodeURIComponent(version)}`,
    decodeWikiChunks,
    { signal },
  );
export const submitWiki = (kb: string, value: WikiEditCreate) =>
  request(
    `/knowledge-bases/${encodeURIComponent(kb)}/wiki/versions`,
    decodeQAAccepted,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(value),
    },
  );
