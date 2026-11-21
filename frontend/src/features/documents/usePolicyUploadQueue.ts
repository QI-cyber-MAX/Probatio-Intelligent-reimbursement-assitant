import { useEffect, useRef, useState } from "react";
import type { DocumentItem, IngestionAccepted } from "../../api/contracts";
import { uploadPolicy } from "../../api/documents";
import { ApiError } from "../../api/http";
import { createUuid } from "../../uuid";
import {
  emptyRegistration,
  policyFormData,
  registrationIdentity,
  resolveRegistration,
  validateRegistration,
  type PolicyRegistration,
  type PolicyRegistrationDraft,
} from "./policy-registration";

type UploadState = "queued" | "uploading" | "accepted" | "failed" | "unknown";
interface Submission {
  file: File;
  registration: PolicyRegistration;
}
export interface PolicyUploadRow {
  id: string;
  file: File;
  draft: PolicyRegistrationDraft;
  selected: boolean;
  state: UploadState;
  errors: Record<string, string>;
  error?: unknown;
  submission?: Submission;
  accepted?: IngestionAccepted;
}
export function usePolicyUploadQueue(
  kb: string,
  candidates: DocumentItem[],
  onAccepted: (value: IngestionAccepted) => void,
) {
  const [rows, setRows] = useState<PolicyUploadRow[]>([]);
  const current = useRef(rows);
  const [running, setRunning] = useState(false);
  const [pausing, setPausing] = useState(false);
  const [started, setStarted] = useState(false);
  const busy = useRef(false);
  const paused = useRef(false);
  const alive = useRef(true);
  const retrying = useRef(new Set<string>());
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
      paused.current = true;
    };
  }, []);
  function update(transform: (values: PolicyUploadRow[]) => PolicyUploadRow[]) {
    current.current = transform(current.current);
    setRows(current.current);
  }
  function patch(id: string, value: Partial<PolicyUploadRow>) {
    update((values) => values.map((row) => row.id === id ? { ...row, ...value } : row));
  }
  function add(files: File[]) {
    if (busy.current) return;
    update((values) => [...values, ...files.map((file): PolicyUploadRow => ({
      id: createUuid(), file, draft: { ...emptyRegistration(candidates), title: file.name.replace(/\.[^.]+$/, ""), version: "CURRENT" },
      selected: true, state: "queued", errors: {},
    }))]);
  }
  function edit(id: string, draft: PolicyRegistrationDraft) {
    if (busy.current) return;
    update((values) => values.map((row) => row.id === id && (row.state === "queued" || row.state === "failed")
      ? { ...row, draft, errors: {}, error: undefined, submission: undefined } : row));
  }
  async function start(retryId?: string) {
    if (busy.current) return;
    if (retryId) retrying.current.add(retryId);
    const pending = current.current.filter((row) => (row.selected && row.state === "queued") || retrying.current.has(row.id));
    if (!pending.length) return;
    const included = current.current.filter((row) => row.selected || row.accepted || row.state === "unknown" || retrying.current.has(row.id));
    const errors = new Map<string, Record<string, string>>();
    const values = new Map<string, PolicyRegistration>();
    for (const row of included) {
      const frozen = row.state === "unknown" || row.state === "accepted";
      values.set(row.id, frozen ? row.submission!.registration : resolveRegistration(row.draft, candidates, true));
      errors.set(row.id, frozen ? {} : validateRegistration(row.draft, candidates, true));
    }
    const groups = new Map<string, PolicyUploadRow[]>();
    for (const row of included) {
      const value = values.get(row.id)!;
      const identity = registrationIdentity(kb, value);
      const group = groups.get(identity) ?? [];
      group.push(row);
      groups.set(identity, group);
    }
    const targets = new Map<string, string>();
    for (const [identity, group] of groups) {
      const first = values.get(group[0]!.id)!;
      // An unresolved first registration stays blocked until its own exact receipt is recovered.
      const unresolved = group.some((row) => row.state === "unknown" && !row.submission!.registration.documentId);
      let known = group.find((row) => row.accepted)?.accepted?.document_id;
      if (!known && !unresolved) {
        known = group.map((row) => values.get(row.id)!.documentId).find(Boolean)
          ?? candidates.find((item) => item.issuer.trim() === first.issuer
            && item.title.trim() === first.title)?.document_id;
      }
      if (known) targets.set(identity, known);
      const currents = group.filter((row) => values.get(row.id)!.version === "CURRENT");
      if (currents.length > 1 || (!known && currents.length === 0)) {
        for (const row of group.filter((item) => item.state !== "accepted" && item.state !== "unknown")) {
          errors.get(row.id)!.version_status = currents.length > 1
            ? "同一制度在本批次最多选择一份新版本。"
            : "首次登记制度需要选择一份新版本。";
        }
      }
    }
    update((items) => items.map((row) => ({ ...row, errors: errors.get(row.id) ?? {} })));
    if ([...errors.values()].some((value) => Object.keys(value).length)) {
      retrying.current.clear();
      return;
    }
    // The validated round owns immutable inputs; edits resume only after this loop has stopped.
    const submissions = new Map(pending.map((row) => [row.id, row.state === "unknown" ? row.submission! : {
      file: row.file, registration: { ...values.get(row.id)! },
    }]));
    busy.current = true;
    paused.current = false;
    setRunning(true);
    setPausing(false);
    setStarted(true);
    const remaining = new Set(pending.map((row) => row.id));
    try {
      while (alive.current && !paused.current && remaining.size) {
        const row = pending.find((item) => {
          if (!remaining.has(item.id)) return false;
          const value = submissions.get(item.id)!.registration;
          return targets.has(registrationIdentity(kb, value)) || value.version === "CURRENT";
        });
        if (!row) break;
        remaining.delete(row.id);
        const submission = submissions.get(row.id)!;
        const identity = registrationIdentity(kb, submission.registration);
        // Frozen unknown retries keep their original title/document_id choice.
        if (row.state !== "unknown" && targets.has(identity)) submission.registration.documentId = targets.get(identity);
        patch(row.id, { state: "uploading", submission, error: undefined });
        try {
          const accepted = await uploadPolicy(kb, policyFormData(submission.file, submission.registration));
          if (!alive.current) return;
          targets.set(identity, accepted.document_id);
          retrying.current.delete(row.id);
          patch(row.id, { state: "accepted", accepted });
          onAccepted(accepted);
        } catch (error) {
          if (!alive.current) return;
          const unknown = row.state === "unknown" || (error instanceof ApiError && error.outcomeUnknown);
          const serverBusy = error instanceof ApiError && error.business?.code === "UPLOAD_BUSY";
          patch(row.id, { state: unknown ? "unknown" : serverBusy ? "queued" : "failed", error,
            errors: error instanceof ApiError ? error.fields : {} });
          if (serverBusy) paused.current = true;
          else retrying.current.delete(row.id);
        }
      }
    } finally {
      busy.current = false;
      if (alive.current) {
        setRunning(false);
        setPausing(false);
      }
    }
  }
  return {
    rows,
    running,
    pausing,
    started,
    add,
    edit,
    start,
    pause() { paused.current = true; setPausing(true); },
    select(id: string, selected: boolean) { if (!busy.current) patch(id, { selected }); },
    remove(id: string) {
      if (!busy.current) update((values) => values.filter((row) => row.id !== id || row.state === "accepted" || row.state === "unknown"));
    },
    applyIssuer(issuer: string) {
      if (!busy.current) update((values) => values.map((row) => row.selected && row.draft.mode === "new" && (row.state === "queued" || row.state === "failed")
        ? { ...row, draft: { ...row.draft, issuer }, errors: {}, error: undefined, submission: undefined } : row));
    },
    canStart: retrying.current.size > 0 || rows.some((row) => row.selected && row.state === "queued"),
    hasPending: rows.some((row) => row.state === "queued" || row.state === "uploading" || row.state === "unknown" || row.state === "failed"),
  };
}
