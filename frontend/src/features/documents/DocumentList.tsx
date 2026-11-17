import type {
  DocumentItem,
  DocumentView,
  IngestionAccepted,
  IngestionJobView,
} from "../../api/contracts";
import { ErrorNotice } from "../../ui/ErrorNotice";
import { SourceStatusCard } from "./SourceStatusCard";
import type { Tracking } from "./useIngestionJobs";

export function DocumentList({
  candidates,
  documents,
  failures,
  tracking,
  selectedHash,
  onRetry,
  onAccepted,
  onJob,
  reload,
  storageUnavailable,
}: {
  candidates: DocumentItem[];
  documents: DocumentView[];
  failures: Record<string, unknown>;
  tracking: Record<string, Tracking>;
  selectedHash: string | null;
  onRetry(): void;
  onAccepted(value: IngestionAccepted): void;
  onJob(value: IngestionJobView): void;
  reload(): void;
  storageUnavailable(): void;
}) {
  return (
    <div className="document-list">
      {candidates.map((candidate, index) => {
        const doc = documents.find(
          (document) => document.document_id === candidate.document_id,
        );
        return (
          <article
            key={candidate.document_id}
            className="document-group"
            aria-label={`${candidate.title} · ${candidate.issuer}`}
          >
            <header className="document-heading">
              <span className="number-tag" aria-hidden="true">
                {String(index + 1).padStart(2, "0")}
              </span>
              <div>
                <h2>{candidate.title}</h2>
                <p className="help">{candidate.issuer}</p>
              </div>
              <span className="group-count">
                {doc ? `${doc.versions.length} 份原件` : "读取原件中…"}
              </span>
            </header>
            {failures[candidate.document_id] ? (
              <ErrorNotice
                error={failures[candidate.document_id]}
                retry={onRetry}
              />
            ) : !doc ? (
              <div className="source-skeleton" aria-label="正在读取原件" />
            ) : (
              doc.versions.map((source) => (
                <SourceStatusCard
                  key={source.source_hash}
                  documentId={doc.document_id}
                  title={doc.title}
                  source={source}
                  tracking={tracking[source.latest_job.job_id]}
                  highlighted={
                    selectedHash === `${doc.document_id}:${source.source_hash}`
                  }
                  onAccepted={onAccepted}
                  onJob={onJob}
                  reload={reload}
                  storageUnavailable={storageUnavailable}
                />
              ))
            )}
          </article>
        );
      })}
    </div>
  );
}
