import type { SourceLocator } from "../../api/contracts";
export function SourceLocatorList({
  locators,
  selected,
  onSelect,
}: {
  locators: SourceLocator[];
  selected: number;
  onSelect(index: number): void;
}) {
  return (
    <fieldset className="source-locators">
      <legend>原文定位</legend>
      {locators.map((locator, index) => (
        <label className="locator-option" key={index}>
          <input
            type="radio"
            name="source-locator"
            checked={selected === index}
            onChange={() => onSelect(index)}
          />
          <span>
            {locator.page_start !== null && (
              <span>
                第 {locator.page_start}
                {locator.page_end !== null &&
                locator.page_end !== locator.page_start
                  ? `—${locator.page_end}`
                  : ""}{" "}
                页
              </span>
            )}
            {locator.sheet_name !== null && (
              <span>
                工作表 {locator.sheet_name}
                {locator.cell_range && ` · ${locator.cell_range}`}
              </span>
            )}
            {locator.sheet_name === null && locator.cell_range !== null && (
              <span>单元格 {locator.cell_range}</span>
            )}
            {locator.paragraph_index !== null && (
              <span>段落索引 {locator.paragraph_index}</span>
            )}
            {locator.section_path.length > 0 && (
              <span>{locator.section_path.join(" / ")}</span>
            )}
            {locator.page_start === null && locator.cell_range === null && (
              <span className="help">未提供页码或单元格定位</span>
            )}
          </span>
        </label>
      ))}
      {!locators.length && <p className="help">未提供原件定位。</p>}
    </fieldset>
  );
}
