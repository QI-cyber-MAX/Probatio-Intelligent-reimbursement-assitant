import type { KnowledgeBaseItem } from "../api/contracts";
import { Icon } from "../ui/Icon";

export function KnowledgeBasePicker({
  bases,
  selected,
  loading,
  onSelect,
  onCreate,
  locked = false,
  nextRound = false,
}: {
  bases: KnowledgeBaseItem[];
  selected: string | null;
  loading: boolean;
  onSelect(id: string): void;
  onCreate(): void;
  locked?: boolean;
  nextRound?: boolean;
}) {
  return (
    <>
      <label className="picker-label" htmlFor="kb-picker">
        {nextRound ? "下一轮知识库" : "知识库"}
      </label>
      <select
        id="kb-picker"
        disabled={loading || locked}
        value={selected ?? ""}
        onChange={(event) => onSelect(event.target.value)}
      >
        <option value="" disabled>
          {loading ? "正在读取…" : "选择知识库"}
        </option>
        {bases.map((base) => (
          <option key={base.kb_id} value={base.kb_id}>
            {base.name}
            {bases.filter((other) => other.name === base.name).length > 1
              ? ` · ${base.kb_id.slice(0, 8)}`
              : ""}
          </option>
        ))}
      </select>
      <button
        type="button"
        className="button secondary full"
        disabled={locked}
        onClick={onCreate}
      >
        <Icon name="plus" />
        创建知识库
      </button>
    </>
  );
}
