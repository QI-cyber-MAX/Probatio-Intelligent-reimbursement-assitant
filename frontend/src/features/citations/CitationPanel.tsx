import { useEffect, useRef, useState } from "react";
import type { Citation } from "../../api/contracts";
import { getDocument } from "../../api/documents";
import { ErrorNotice } from "../../ui/ErrorNotice";
import { Icon } from "../../ui/Icon";
import { OriginalPreview } from "./OriginalPreview";
import { SourceLocatorList } from "./SourceLocatorList";
export interface CitationSelection {
  jobId: string;
  messageId: string | null;
  citation: Citation;
}
export function CitationPanel({
  selection,
  onClose,
}: {
  selection: CitationSelection;
  onClose(): void;
}) {
  const titles = useRef(new Map<string, string>());
  return (
    <div className="citation-panel-content">
      <header className="citation-panel-heading">
        <h2>引用原文</h2>
        <button
          className="icon-button"
          aria-label="关闭引用"
          onClick={onClose}
        >
          <Icon name="close" />
        </button>
      </header>
      <SelectedCitation
        key={`${selection.jobId}:${selection.citation.number}`}
        selection={selection}
        titles={titles.current}
      />
    </div>
  );
}
function SelectedCitation({
  selection,
  titles,
}: {
  selection: CitationSelection;
  titles: Map<string, string>;
}) {
  const { citation } = selection;
  const [index, setIndex] = useState(0);
  const [revision, setRevision] = useState(0);
  const [title, setTitle] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);
  const locator = citation.locators[index];
  const documentId = locator?.document_id;
  useEffect(() => {
    const controller = new AbortController();
    setError(null);
    setTitle(documentId ? (titles.get(documentId) ?? null) : null);
    if (documentId && !titles.has(documentId))
      void getDocument(documentId, controller.signal)
        .then((doc) => {
          if (!controller.signal.aborted) {
            titles.set(documentId, doc.title);
            setTitle(doc.title);
          }
        })
        .catch((failure) => {
          if (!controller.signal.aborted) setError(failure);
        });
    return () => controller.abort();
  }, [documentId, titles, revision]);
  return (
    <>
      <div className="citation-source-heading">
        <span className="cut-number">[{citation.number}]</span>
        <h3>{title ?? "制度原件"}</h3>
      </div>
      {error !== null && (
        <ErrorNotice error={error} retry={() => setRevision((n) => n + 1)} />
      )}
      <section className="citation-source-text">
        <h3>制度原文</h3>
        <blockquote className="message-text">{citation.source_text}</blockquote>
      </section>
      <SourceLocatorList
        locators={citation.locators}
        selected={index}
        onSelect={setIndex}
      />
      {locator && (
        <OriginalPreview
          key={`${locator.document_id}:${locator.source_hash}`}
          locator={locator}
        />
      )}
    </>
  );
}
