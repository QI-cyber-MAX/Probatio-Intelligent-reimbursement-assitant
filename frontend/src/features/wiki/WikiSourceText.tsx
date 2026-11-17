import { useEffect, useState } from "react";
import type { WikiChunkOption, WikiPage } from "../../api/contracts";
import { getWikiChunks } from "../../api/wiki";
import { ErrorNotice } from "../../ui/ErrorNotice";

export function WikiSourceText({
  kb,
  version,
  page,
}: {
  kb: string;
  version: string;
  page: WikiPage;
}) {
  const [opened, setOpened] = useState(false);
  const [chunks, setChunks] = useState<WikiChunkOption[] | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    if (!opened) return;
    const controller = new AbortController();
    void getWikiChunks(kb, page.page_id, version, controller.signal)
      .then((value) => {
        if (!controller.signal.aborted) {
          setChunks(value);
          setError(null);
        }
      })
      .catch((failure) => {
        if (!controller.signal.aborted) setError(failure);
      });
    return () => controller.abort();
  }, [kb, page.page_id, version, opened, attempt]);
  const selected = new Set(
    page.statements.flatMap((s) => s.chunk_refs.map((r) => r.chunk_id)),
  );
  return (
    <details onToggle={(event) => setOpened(event.currentTarget.open)}>
      <summary>查看引用原文</summary>
      {error !== null && (
        <ErrorNotice error={error} retry={() => setAttempt((v) => v + 1)} />
      )}
      {chunks === null && error === null && (
        <p role="status">正在读取引用原文…</p>
      )}
      {chunks
        ?.filter((chunk) => selected.has(chunk.chunk_id))
        .map((chunk) => (
          <section key={chunk.chunk_id}>
            <h3>原文 {chunk.ordinal + 1}</h3>
            {chunk.locators.map((locator, i) => (
              <p key={i} className="help">
                {locator.page_start !== null
                  ? `第 ${locator.page_start} 页 · `
                  : ""}
                {locator.sheet_name}
                {locator.cell_range ? ` · ${locator.cell_range}` : ""}
                {locator.section_path.join(" / ")}
              </p>
            ))}
            <p className="wiki-text">{chunk.body_text}</p>
          </section>
        ))}
    </details>
  );
}
