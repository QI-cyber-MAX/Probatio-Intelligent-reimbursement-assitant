import { useEffect, useState } from "react";
import {
  wikiTopics,
  type WikiChunkOption,
  type WikiEditCreate,
  type WikiStatement,
  type WikiTopic,
  type WikiVersion,
} from "../../api/contracts";
import { getWikiChunks } from "../../api/wiki";
import { topicNames } from "./labels";
import { ErrorNotice } from "../../ui/ErrorNotice";
import { Field } from "../../ui/Field";
import { createUuid } from "../../uuid";

type Option = { id: string; title: string };
function References({
  id,
  label,
  options,
  values,
  change,
}: {
  id: string;
  label: string;
  options: Option[];
  values: string[];
  change: (values: string[]) => void;
}) {
  return (
    <fieldset id={id} tabIndex={-1} className="wiki-references">
      <legend>{label}</legend>
      {options.map((option) => (
        <label key={option.id}>
          <input
            type="checkbox"
            checked={values.includes(option.id)}
            onChange={(event) =>
              change(
                event.target.checked
                  ? [...values, option.id]
                  : values.filter((v) => v !== option.id),
              )
            }
          />
          {option.title}
        </label>
      ))}
    </fieldset>
  );
}
function StatementsEditor({
  prefix,
  statements,
  change,
  references,
  chunks,
  source,
  errors,
}: {
  prefix: string;
  statements: WikiStatement[];
  change: (s: WikiStatement[]) => void;
  references: Option[];
  chunks: WikiChunkOption[];
  source: boolean;
  errors: Record<string, string>;
}) {
  const replace = (topic: WikiTopic, value: WikiStatement | null) => {
    const items = statements.filter((s) => s.topic !== topic);
    if (value) items.push(value);
    change(wikiTopics.flatMap((t) => items.filter((s) => s.topic === t)));
  };
  return (
    <div>
      {wikiTopics.map((topic) => {
        const index = statements.findIndex((s) => s.topic === topic),
          item = statements[index];
        const id = `${prefix}.${index}`;
        return (
          <section key={topic} className="wiki-topic">
            <div className="wiki-section-heading">
              <h3>{topicNames[topic]}</h3>
              <button
                type="button"
                className="button secondary compact"
                onClick={() =>
                  replace(
                    topic,
                    item
                      ? null
                      : {
                          topic,
                          kind:
                            topic === "relationship"
                              ? "relation"
                              : topic === "open_question"
                                ? "open_question"
                                : "summary",
                          evidence_level: source ? "direct" : "derived",
                          text: "",
                          chunk_refs: [],
                          related_page_ids: [],
                        },
                  )
                }
              >
                {item ? "移除此主题" : "添加此主题"}
              </button>
            </div>
            {item && (
              <>
                <Field
                  id={`${id}.text`}
                  label={`${topicNames[topic]}正文`}
                  error={errors[`${id}.text`]}
                >
                  <textarea
                    id={`${id}.text`}
                    rows={4}
                    value={item.text}
                    aria-invalid={!!errors[`${id}.text`]}
                    aria-describedby={
                      errors[`${id}.text`] ? `${id}.text-error` : undefined
                    }
                    onChange={(e) =>
                      replace(topic, { ...item, text: e.target.value })
                    }
                  />
                </Field>
                <Field id={`${id}.evidence_level`} label="依据类型">
                  <select
                    id={`${id}.evidence_level`}
                    value={item.evidence_level}
                    onChange={(e) =>
                      replace(topic, {
                        ...item,
                        evidence_level: e.target
                          .value as WikiStatement["evidence_level"],
                      })
                    }
                  >
                    <option value="direct">直接依据</option>
                    <option value="derived">综合归纳</option>
                    {topic === "open_question" && (
                      <option value="inferred">待确认推断</option>
                    )}
                  </select>
                </Field>
                {source ? (
                  <References
                    id={`${id}.chunk_refs`}
                    label="原文引用"
                    options={chunks.map((c) => ({
                      id: c.chunk_id,
                      title: `原文 ${c.ordinal + 1} · ${c.locators.flatMap((l) => l.section_path).join(" / ")}`,
                    }))}
                    values={item.chunk_refs.map((r) => r.chunk_id)}
                    change={(ids) =>
                      replace(topic, {
                        ...item,
                        chunk_refs: ids.map((key) => {
                          const c = chunks.find((c) => c.chunk_id === key)!;
                          return {
                            generation_id: c.generation_id,
                            chunk_id: c.chunk_id,
                          };
                        }),
                      })
                    }
                  />
                ) : (
                  <References
                    id={`${id}.related_page_ids`}
                    label="来源页引用"
                    options={references}
                    values={item.related_page_ids}
                    change={(ids) =>
                      replace(topic, { ...item, related_page_ids: ids })
                    }
                  />
                )}
              </>
            )}
          </section>
        );
      })}
    </div>
  );
}
function SourceEditor({
  kb,
  version,
  item,
  prefix,
  change,
  errors,
}: {
  kb: string;
  version: WikiVersion;
  item: WikiEditCreate["source_pages"][number];
  prefix: string;
  change: (s: WikiStatement[]) => void;
  errors: Record<string, string>;
}) {
  const [chunks, setChunks] = useState<WikiChunkOption[]>([]),
    [error, setError] = useState<unknown>(null);
  const [opened, setOpened] = useState(false),
    [reload, setReload] = useState(0);
  useEffect(() => {
    if (!opened) return;
    const controller = new AbortController();
    void getWikiChunks(kb, item.page_id, version.job_id, controller.signal)
      .then((values) => {
        if (!controller.signal.aborted) {
          setChunks(values);
          setError(null);
        }
      })
      .catch((failure) => {
        if (!controller.signal.aborted) setError(failure);
      });
    return () => controller.abort();
  }, [kb, item.page_id, version.job_id, opened, reload]);
  return (
    <details onToggle={(e) => setOpened(e.currentTarget.open)}>
      <summary>{version.pages[item.page_id]?.title ?? item.page_id}</summary>
      <div className="wiki-editor-body">
        {error !== null && (
          <ErrorNotice error={error} retry={() => setReload((v) => v + 1)} />
        )}
        {opened && !chunks.length && error === null ? (
          <p role="status">正在读取原文引用…</p>
        ) : (
          <StatementsEditor
            prefix={prefix}
            statements={item.statements}
            change={change}
            references={[]}
            chunks={chunks}
            source
            errors={errors}
          />
        )}
        {chunks.map((c) => (
          <details key={c.chunk_id}>
            <summary>原文 {c.ordinal + 1}</summary>
            <p className="wiki-text">{c.body_text}</p>
          </details>
        ))}
      </div>
    </details>
  );
}
export function WikiEditor({
  kb,
  version,
  value,
  change,
  disabled,
  errors,
}: {
  kb: string;
  version: WikiVersion;
  value: WikiEditCreate;
  change: (v: WikiEditCreate) => void;
  disabled: boolean;
  errors: Record<string, string>;
}) {
  const sources = value.source_pages.map((s) => ({
    id: s.page_id,
    title: version.pages[s.page_id]?.title ?? s.page_id,
  }));
  const patchScene = (
    index: number,
    patch: Partial<WikiEditCreate["scenes"][number]>,
  ) =>
    change({
      ...value,
      scenes: value.scenes.map((s, i) =>
        i === index ? { ...s, ...patch } : s,
      ),
    });
  return (
    <fieldset disabled={disabled} className="wiki-editor">
      <legend>完整导航编辑</legend>
      <section>
        <h2>概览</h2>
        <Field
          id="overview.business_context.text"
          label="知识库业务上下文"
          error={errors["overview.business_context.text"]}
        >
          <textarea
            id="overview.business_context.text"
            rows={4}
            value={value.overview.business_context.text}
            onChange={(e) =>
              change({
                ...value,
                overview: {
                  ...value.overview,
                  business_context: {
                    ...value.overview.business_context,
                    text: e.target.value,
                  },
                },
              })
            }
          />
        </Field>
        <References
          id="overview.business_context.related_scene_refs"
          label="业务上下文引用场景"
          options={value.scenes.map((s) => ({
            id: s.scene_ref,
            title: s.title || "未命名场景",
          }))}
          values={value.overview.business_context.related_scene_refs}
          change={(ids) =>
            change({
              ...value,
              overview: {
                ...value.overview,
                business_context: {
                  ...value.overview.business_context,
                  related_scene_refs: ids,
                },
              },
            })
          }
        />
      </section>
      <section id="scenes" tabIndex={-1}>
        <div className="wiki-section-heading">
          <h2>场景与制度分工</h2>
          <button
            type="button"
            className="button secondary"
            onClick={() => {
              const ref = createUuid();
              change({
                ...value,
                scenes: [
                  ...value.scenes,
                  {
                    scene_ref: ref,
                    page_id: null,
                    title: "",
                    source_page_ids: [],
                    statements: [],
                  },
                ],
                overview: {
                  ...value.overview,
                  scenarios: [
                    ...value.overview.scenarios,
                    { scene_ref: ref, text: "" },
                  ],
                },
              });
            }}
          >
            新增场景
          </button>
        </div>
        {value.scenes.map((scene, index) => (
          <details key={scene.scene_ref} open className="wiki-scene">
            <summary>{scene.title || "新场景"}</summary>
            <div className="wiki-editor-body">
              <Field
                id={`scenes.${index}.title`}
                label="场景名称"
                error={errors[`scenes.${index}.title`]}
              >
                <input
                  id={`scenes.${index}.title`}
                  value={scene.title}
                  onChange={(e) => patchScene(index, { title: e.target.value })}
                />
              </Field>
              <References
                id={`scenes.${index}.source_page_ids`}
                label="场景成员"
                options={sources}
                values={scene.source_page_ids}
                change={(ids) => patchScene(index, { source_page_ids: ids })}
              />
              <Field
                id={`overview.scenarios.${index}.text`}
                label="概览中的场景介绍"
                error={errors[`overview.scenarios.${index}.text`]}
              >
                <textarea
                  id={`overview.scenarios.${index}.text`}
                  value={
                    value.overview.scenarios.find(
                      (s) => s.scene_ref === scene.scene_ref,
                    )?.text ?? ""
                  }
                  onChange={(e) =>
                    change({
                      ...value,
                      overview: {
                        ...value.overview,
                        scenarios: value.overview.scenarios.map((s) =>
                          s.scene_ref === scene.scene_ref
                            ? { ...s, text: e.target.value }
                            : s,
                        ),
                      },
                    })
                  }
                />
              </Field>
              <StatementsEditor
                prefix={`scenes.${index}.statements`}
                statements={scene.statements}
                change={(statements) => patchScene(index, { statements })}
                references={sources.filter(
                  (s) =>
                    scene.source_page_ids.includes(s.id) ||
                    scene.statements.some((t) =>
                      t.related_page_ids.includes(s.id),
                    ),
                )}
                chunks={[]}
                source={false}
                errors={errors}
              />
              <button
                type="button"
                className="button secondary danger-text"
                onClick={() =>
                  change({
                    ...value,
                    scenes: value.scenes.filter((_, i) => i !== index),
                    overview: {
                      business_context: {
                        ...value.overview.business_context,
                        related_scene_refs:
                          value.overview.business_context.related_scene_refs.filter(
                            (r) => r !== scene.scene_ref,
                          ),
                      },
                      scenarios: value.overview.scenarios.filter(
                        (s) => s.scene_ref !== scene.scene_ref,
                      ),
                    },
                  })
                }
              >
                删除场景
              </button>
              <p className="help">
                删除后请检查业务上下文文字、引用和剩余场景的成员覆盖。
              </p>
            </div>
          </details>
        ))}
      </section>
      <section id="source_pages" tabIndex={-1}>
        <h2>来源页</h2>
        {value.source_pages.map((source, index) => (
          <SourceEditor
            key={source.page_id}
            kb={kb}
            version={version}
            item={source}
            prefix={`source_pages.${index}.statements`}
            change={(statements) =>
              change({
                ...value,
                source_pages: value.source_pages.map((s, i) =>
                  i === index ? { ...s, statements } : s,
                ),
              })
            }
            errors={errors}
          />
        ))}
      </section>
    </fieldset>
  );
}
