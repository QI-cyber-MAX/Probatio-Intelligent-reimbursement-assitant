import { useState } from "react";
import type {
  KnowledgeBaseItem,
  WikiEditCreate,
  WikiPage as Page,
  WikiVersion,
} from "../../api/contracts";
import { ApiError } from "../../api/http";
import { ErrorNotice } from "../../ui/ErrorNotice";
import { WikiEditor } from "./WikiEditor";
import { WikiSourceText } from "./WikiSourceText";
import { topicNames } from "./labels";
import { useWiki } from "./useWiki";

function preview(
  version: WikiVersion,
  edit: WikiEditCreate,
): Record<string, Page> {
  const ids = Object.fromEntries(
    edit.scenes.map((s) => [s.scene_ref, s.page_id ?? s.scene_ref]),
  );
  const pages: Record<string, Page> = {};
  for (const source of edit.source_pages)
    pages[source.page_id] = {
      page_id: source.page_id,
      kind: "document",
      title: version.pages[source.page_id]?.title ?? "来源页",
      statements: source.statements,
      child_page_ids: [],
    };
  for (const scene of edit.scenes) {
    const id = ids[scene.scene_ref]!;
    pages[id] = {
      page_id: id,
      kind: "synthesis",
      title: scene.title || "未命名场景",
      statements: scene.statements,
      child_page_ids: scene.source_page_ids,
    };
  }
  pages.overview = {
    page_id: "overview",
    kind: "overview",
    title: version.pages.overview!.title,
    statements: [
      {
        kind: "summary",
        topic: "business_context",
        evidence_level: "derived",
        text: edit.overview.business_context.text,
        chunk_refs: [],
        related_page_ids: edit.overview.business_context.related_scene_refs.map(
          (r) => ids[r]!,
        ),
      },
      ...edit.overview.scenarios.map((s) => ({
        kind: "relation" as const,
        topic: "relationship" as const,
        evidence_level: "derived" as const,
        text: s.text,
        chunk_refs: [],
        related_page_ids: [ids[s.scene_ref]!],
      })),
    ],
    child_page_ids: edit.scenes.map((s) => ids[s.scene_ref]!),
  };
  return pages;
}
export function WikiPage({ base }: { base: KnowledgeBaseItem }) {
  const wiki = useWiki(base.kb_id);
  const [selected, setSelected] = useState("overview"),
    [showPreview, setShowPreview] = useState(false);
  const version = wiki.workspace?.published_version;
  const pages = version
    ? showPreview && wiki.draft
      ? preview(version, wiki.draft)
      : version.pages
    : {};
  const page = pages[selected] ?? pages.overview;
  const errors = wiki.error instanceof ApiError ? wiki.error.fields : {};
  function focusField(path: string) {
    const parts = path.split(".");
    let element: HTMLElement | null = null;
    while (parts.length && !element) {
      element = document.getElementById(parts.join("."));
      parts.pop();
    }
    if (element) {
      for (
        let parent = element.parentElement;
        parent;
        parent = parent.parentElement
      )
        if (parent instanceof HTMLDetailsElement) parent.open = true;
      element.focus();
      element.scrollIntoView({ block: "center" });
    }
  }
  return (
    <>
      <div className="page-heading">
        <div>
          <p className="eyebrow">KNOWLEDGE GUIDE</p>
          <h1 id="page-title" tabIndex={-1}>
            知识导航
          </h1>
          <p className="muted">{base.name} · 场景、制度分工与原文入口</p>
        </div>
        {version && !wiki.draft && (
          <button
            type="button"
            className="button primary"
            disabled={wiki.locked}
            onClick={() => {
              void wiki.start();
            }}
          >
            编辑导航
          </button>
        )}
      </div>
      {wiki.error !== null && (
        <ErrorNotice
          error={wiki.error}
          retry={() => {
            void wiki.refresh();
          }}
        />
      )}
      {Object.keys(errors).length > 0 && (
        <div role="alert" className="notice danger" tabIndex={-1}>
          <p>请修正以下字段：</p>
          <ul>
            {Object.entries(errors).map(([path, message]) => (
              <li key={path}>
                <a
                  href={`#${path}`}
                  onClick={(e) => {
                    e.preventDefault();
                    setShowPreview(false);
                    requestAnimationFrame(() => focusField(path));
                  }}
                >
                  {path}：{message}
                </a>
              </li>
            ))}
          </ul>
        </div>
      )}
      {wiki.storageWarning && (
        <p className="notice warning">
          浏览器无法保存草稿，请保留当前页面以便核对提交结果。
        </p>
      )}
      {wiki.notice && (
        <p role="status" className="notice">
          {wiki.notice}
        </p>
      )}
      {wiki.workspace?.active_job && (
        <p role="status" className="notice">
          {wiki.workspace.active_job.operation === "build"
            ? "知识导航正在自动构建，编辑和保存暂不可用。"
            : "知识导航正在发布，正在核对任务结果。"}
          （{wiki.workspace.active_job.status}）
        </p>
      )}
      {wiki.sourcesStale && (
        <p className="notice warning">来源已更新，需先完成导航构建后再编辑。</p>
      )}
      {wiki.stale && (
        <div className="notice warning">
          <p>
            发布版本已变化，旧草稿已冻结。可对照当前发布内容，手工迁移修改。
          </p>
          <button
            className="button secondary"
            type="button"
            onClick={() => setShowPreview((v) => !v)}
          >
            {showPreview ? "查看当前发布内容" : "查看旧草稿预览"}
          </button>
        </div>
      )}
      {wiki.draft && version && (
        <div className="wiki-toolbar">
          <button
            className="button secondary"
            type="button"
            onClick={() => setShowPreview((v) => !v)}
          >
            {showPreview ? "返回编辑" : "预览草稿"}
          </button>
          <button
            type="button"
            className="button secondary"
            disabled={wiki.busy || wiki.pending}
            onClick={() => {
              wiki.discard();
              setShowPreview(false);
            }}
          >
            放弃草稿并读取当前版本
          </button>
          <button
            type="button"
            className="button primary"
            disabled={wiki.locked}
            onClick={() => {
              void wiki.save();
            }}
          >
            保存并发布
          </button>
          {wiki.pending && (
            <button
              type="button"
              className="button secondary"
              disabled={wiki.busy}
              onClick={() => {
                void wiki.save();
              }}
            >
              核对并重试提交
            </button>
          )}
        </div>
      )}
      {!wiki.workspace ? (
        <p role="status">正在读取知识导航…</p>
      ) : !version ? (
        <div className="empty-state">
          <h2>尚无已发布导航</h2>
          <p className="muted">制度来源完成构建后将在此显示。</p>
        </div>
      ) : wiki.draft && !showPreview && !wiki.stale ? (
        <WikiEditor
          kb={base.kb_id}
          version={version}
          value={wiki.draft}
          change={wiki.update}
          disabled={wiki.locked}
          errors={errors}
        />
      ) : (
        <div className="wiki-layout">
          <nav aria-label="导航页面" className="wiki-directory">
            <button
              className="button secondary"
              type="button"
              aria-current={selected === "overview" ? "page" : undefined}
              onClick={() => setSelected("overview")}
            >
              知识库概览
            </button>
            <h2>报销场景</h2>
            {pages.overview?.child_page_ids.map((id) => (
              <button
                className="button secondary"
                type="button"
                key={id}
                aria-current={selected === id ? "page" : undefined}
                onClick={() => setSelected(id)}
              >
                {pages[id]?.title}
              </button>
            ))}
            <h2>来源制度</h2>
            {Object.values(pages)
              .filter((p) => p.kind === "document")
              .map((p) => (
                <button
                  className="button secondary"
                  type="button"
                  key={p.page_id}
                  aria-current={selected === p.page_id ? "page" : undefined}
                  onClick={() => setSelected(p.page_id)}
                >
                  {p.title}
                </button>
              ))}
          </nav>
          <article
            className="wiki-content"
            aria-label={showPreview ? "草稿预览" : "已发布导航"}
          >
            {page && (
              <>
                <h2>{page.title}</h2>
                {page.statements.map((statement, i) => (
                  <section key={i}>
                    <h3>
                      {page.kind === "overview" &&
                      statement.topic === "relationship"
                        ? pages[statement.related_page_ids[0]!]?.title
                        : topicNames[statement.topic]}
                    </h3>
                    <p className="wiki-text">{statement.text}</p>
                    <div className="wiki-links">
                      {statement.related_page_ids.map((id) => (
                        <button
                          type="button"
                          className="button secondary compact"
                          key={id}
                          onClick={() => setSelected(id)}
                        >
                          {pages[id]?.title}
                        </button>
                      ))}
                    </div>
                    {statement.chunk_refs.length > 0 && (
                      <p className="help">
                        原文引用：{statement.chunk_refs.length} 处。
                      </p>
                    )}
                  </section>
                ))}
                {page.kind === "synthesis" && (
                  <div className="wiki-links">
                    {page.child_page_ids.map((id) => (
                      <button
                        type="button"
                        className="button secondary"
                        key={id}
                        onClick={() => setSelected(id)}
                      >
                        {pages[id]?.title}
                      </button>
                    ))}
                  </div>
                )}
                {page.kind === "document" && (
                  <WikiSourceText
                    key={`${version.job_id}:${page.page_id}`}
                    kb={base.kb_id}
                    version={showPreview && wiki.draft ? wiki.draft.base_job_id : version.job_id}
                    page={page}
                  />
                )}
              </>
            )}
          </article>
        </div>
      )}
    </>
  );
}
