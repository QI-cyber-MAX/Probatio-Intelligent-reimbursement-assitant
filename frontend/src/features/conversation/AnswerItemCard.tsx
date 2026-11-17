import type { AnswerItem, Citation } from "../../api/contracts";
import { StatusBadge } from "../../ui/StatusBadge";
const coverageLabels = {
  COMPLETE: "资料完整",
  PARTIAL: "部分有依据",
  UNSUPPORTED: "暂无充分依据",
};
const gapLabels = {
  USER_FACT: "缺少用户信息",
  POLICY_MISSING: "缺少制度依据",
  CONFLICT: "依据存在冲突",
};
export function AnswerItemCard({
  item,
  index,
  citationsByChunk,
  onCitation,
  selectedNumber,
}: {
  item: AnswerItem;
  index: number;
  citationsByChunk: ReadonlyMap<string, Citation>;
  onCitation(number: number): void;
  selectedNumber?: number;
}) {
  const sourceButtons = (chunkIds: string[]) =>
    chunkIds.map((key) => {
      const citation = citationsByChunk.get(key)!;
      return (
        <button
          key={key}
          type="button"
          className="button secondary citation-button"
          aria-label={`引用 ${citation.number}`}
          aria-pressed={selectedNumber === citation.number}
          onClick={() => onCitation(citation.number)}
        >
          <span className="citation-number">[{citation.number}]</span>查看原文
        </button>
      );
    });
  return (
    <section className="answer-item">
      <header className="answer-item-heading">
        <span className="cut-number">{String(index + 1).padStart(2, "0")}</span>
        <h2>{item.question}</h2>
        <StatusBadge
          tone={item.coverage === "COMPLETE" ? "success" : "warning"}
        >
          {coverageLabels[item.coverage]}
        </StatusBadge>
      </header>
      <p className="message-text">{item.text}</p>
      {item.citation_numbers.length > 0 && (
        <div className="citation-buttons" aria-label="本项引用">
          {item.citation_numbers.map((number) => (
            <button
              key={number}
              className="button secondary citation-button"
              aria-label={`引用 ${number}`}
              aria-pressed={selectedNumber === number}
              onClick={() => onCitation(number)}
            >
              <span className="citation-number">[{number}]</span>查看原文
            </button>
          ))}
        </div>
      )}
      {item.gaps.length > 0 && (
        <div className="answer-gaps">
          <h3>仍需确认内容</h3>
          {item.gaps.map((gap, i) => (
            <div key={i}>
              <p>
                <span className="gap-label">{gapLabels[gap.kind]}</span>：
                {gap.description}
              </p>
              {gap.evidence_chunk_ids.length > 0 && (
                <div className="citation-buttons" aria-label="缺口相关原文">
                  {sourceButtons(gap.evidence_chunk_ids)}
                </div>
              )}
            </div>
          ))}
        </div>
      )}
      {item.claims.length > 0 && (
        <details className="answer-reasoning">
          <summary>判断依据</summary>
          {item.claims.map((claim) => (
            <div key={claim.claim_id}>
              <p className="message-text">{claim.text}</p>
              <div className="citation-buttons" aria-label="命题相关原文">
                {sourceButtons(claim.evidence_chunk_ids)}
              </div>
            </div>
          ))}
        </details>
      )}
    </section>
  );
}
