import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type MouseEvent,
} from "react";
import type {
  Citation,
  KnowledgeBase,
  KnowledgeBaseItem,
} from "../api/contracts";
import { listKnowledgeBases } from "../api/documents";
import { LibraryPage } from "../features/documents/LibraryPage";
import { ConversationView } from "../features/conversation/ConversationView";
import { WikiPage } from "../features/wiki/WikiPage";
import {
  CitationPanel,
  type CitationSelection,
} from "../features/citations/CitationPanel";
import {
  readRecentConversations,
  saveRecentConversations,
} from "../storage/recent-conversations";
import { Dialog } from "../ui/Dialog";
import { ErrorNotice } from "../ui/ErrorNotice";
import { Icon } from "../ui/Icon";
import { CreateKnowledgeBaseDialog } from "./CreateKnowledgeBaseDialog";
import { KnowledgeBasePicker } from "./KnowledgeBasePicker";
import { conversationUrl, pageUrl, readRoute } from "./routes";
import { RecentConversations } from "./RecentConversations";

export function AppShell() {
  const [route, setRoute] = useState(readRoute);
  const leaveGuard = useRef<(() => boolean) | null>(null);
  const currentUrl = useRef(window.location.pathname + window.location.search);
  const setLeaveGuard = useCallback((guard: (() => boolean) | null) => { leaveGuard.current = guard; }, []);
  const [bases, setBases] = useState<KnowledgeBaseItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<unknown>(null);
  const [revision, setRevision] = useState(0);
  const [creating, setCreating] = useState(false);
  const [mobileOpen, setMobileOpen] = useState(false);
  const [restoredRecent] = useState(() => {
    let unavailable = false;
    const values = readRecentConversations(() => { unavailable = true; });
    return { values, unavailable };
  });
  const [recent, setRecent] = useState(restoredRecent.values);
  const [storageWarning, setStorageWarning] = useState(restoredRecent.unavailable);
  const [chatKey, setChatKey] = useState(0);
  const [chatBusy, setChatBusy] = useState(false);
  const [citation, setCitation] = useState<CitationSelection | null>(null);
  const citationTrigger = useRef<HTMLElement | null>(null);
  const citationPanel = useRef<HTMLElement | null>(null);
  const [wide, setWide] = useState(
    () => window.matchMedia("(min-width: 1200px)").matches,
  );
  const refresh = useCallback(() => setRevision((value) => value + 1), []);
  const navigate = useCallback((url: string) => {
    const previous = readRoute();
    if (url !== currentUrl.current && leaveGuard.current && !leaveGuard.current()) return;
    currentUrl.current = url;
    window.history.pushState(null, "", url);
    const next = readRoute();
    if (
      previous.page !== next.page ||
      (previous.page === "chat" &&
        next.page === "chat" &&
        previous.conversationId !== next.conversationId)
    ) {
      setChatKey((value) => value + 1);
      setCitation(null);
      setChatBusy(false);
    }
    setRoute(next);
    setMobileOpen(false);
  }, []);
  useEffect(() => {
    const update = () => {
      if (leaveGuard.current && !leaveGuard.current()) {
        window.history.pushState(null, "", currentUrl.current);
        return;
      }
      currentUrl.current = window.location.pathname + window.location.search;
      setRoute(readRoute());
      setChatKey((value) => value + 1);
      setCitation(null);
      setChatBusy(false);
    };
    window.addEventListener("popstate", update);
    if (window.location.pathname === "/") {
      window.history.replaceState(null, "", pageUrl("library", readRoute().kb));
    }
    return () => window.removeEventListener("popstate", update);
  }, []);
  useEffect(() => {
    const media = window.matchMedia("(min-width: 1200px)");
    const change = () => setWide(media.matches);
    media.addEventListener("change", change);
    return () => media.removeEventListener("change", change);
  }, []);
  const conversationId = route.page === "chat" ? route.conversationId : null;
  const kb =
    route.kb ??
    (conversationId
      ? (recent.find((item) => item.conversation_id === conversationId)
          ?.kb_id ?? null)
      : null);
  const remember = useCallback((id: string, selectedKb: string | null) => {
    setRecent((values) => {
      const next = [
        {
          conversation_id: id,
          kb_id: selectedKb,
          last_opened_at: new Date().toISOString(),
        },
        ...values.filter((item) => item.conversation_id !== id),
      ].slice(0, 20);
      if (!saveRecentConversations(next)) setStorageWarning(true);
      return next;
    });
  }, []);
  useEffect(() => {
    if (conversationId) remember(conversationId, kb);
  }, [conversationId, kb, remember]);
  const conversationCreated = useCallback(
    (id: string) => {
      remember(id, kb);
      window.history.pushState(null, "", conversationUrl(id, kb));
      setRoute(readRoute());
    },
    [remember, kb],
  );
  const selectCitation = useCallback(
    (jobId: string, messageId: string | null, selected: Citation) => {
      citationTrigger.current =
        document.activeElement instanceof HTMLElement
          ? document.activeElement
          : null;
      setCitation({ jobId, messageId, citation: selected });
    },
    [],
  );
  const closeCitation = useCallback(() => {
    setCitation(null);
    if (citationTrigger.current?.isConnected) citationTrigger.current.focus();
  }, []);
  const citationOpen = citation !== null;
  useEffect(() => {
    if (citationOpen && wide)
      citationPanel.current?.querySelector("button")?.focus();
  }, [citationOpen, wide]);
  useEffect(() => {
    document.title = `Probatio · ${route.page === "library" ? "知识库与制度" : route.page === "wiki" ? "知识导航" : "制度问答"}`;
    document.getElementById("page-title")?.focus();
  }, [route.page, chatKey]);
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    void listKnowledgeBases(controller.signal)
      .then((values) => {
        if (!controller.signal.aborted) setBases(values);
      })
      .catch((failure) => {
        if (!controller.signal.aborted) setError(failure);
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [revision]);
  const selected = bases.find((base) => base.kb_id === kb);
  function select(id: string) {
    navigate(
      conversationId
        ? conversationUrl(conversationId, id)
        : pageUrl(route.page, id),
    );
  }
  function created(base: KnowledgeBase) {
    setBases((previous) => [...previous, base]);
    setCreating(false);
    navigate(pageUrl("library", base.kb_id));
  }
  const follow = (event: MouseEvent<HTMLAnchorElement>, url: string) => {
    if (
      event.button !== 0 ||
      event.metaKey ||
      event.ctrlKey ||
      event.shiftKey ||
      event.altKey
    )
      return;
    event.preventDefault();
    navigate(url);
  };
  const picker = (
    <KnowledgeBasePicker
      bases={bases}
      selected={kb}
      loading={loading}
      locked={route.page === "chat" && chatBusy}
      nextRound={!!conversationId}
      onSelect={select}
      onCreate={() => {
        setMobileOpen(false);
        setCreating(true);
      }}
    />
  );
  const recents = route.page === "chat" && (
    <RecentConversations
      values={recent}
      current={conversationId}
      onNew={() => {
        setChatKey((value) => value + 1);
        setCitation(null);
        navigate(pageUrl("chat", kb));
      }}
      onOpen={(id, selectedKb) => navigate(conversationUrl(id, selectedKb))}
      onRemove={(id) =>
        setRecent((values) => {
          const next = values.filter((item) => item.conversation_id !== id);
          if (!saveRecentConversations(next)) setStorageWarning(true);
          return next;
        })
      }
    />
  );
  return (
    <div
      className={`app-shell${route.page === "chat" ? " chat-shell" : ""}${route.page === "chat" && citationOpen ? " citation-open" : ""}`}
    >
      <a className="skip-link" href="#main">
        跳到主内容
      </a>
      <header className="topbar">
        <a
          className="brand"
          href={pageUrl("library", kb)}
          onClick={(event) => follow(event, pageUrl("library", kb))}
          aria-label="Probatio 知识库与制度"
        >
          <svg viewBox="0 0 40 44" fill="none" aria-hidden="true">
            <path
              d="M4 2h23l9 10v30H4Z"
              stroke="currentColor"
              strokeWidth="1.5"
            />
            <path
              d="m14 40 20-20M23 40l11-11"
              stroke="var(--action-bg)"
              strokeWidth="1.5"
            />
          </svg>
          <span>Probatio</span>
        </a>
        <nav aria-label="主导航">
          {(["library", "wiki", "chat"] as const).map((page) => (
            <a
              key={page}
              href={pageUrl(page, kb)}
              aria-current={route.page === page ? "page" : undefined}
              onClick={(event) => follow(event, pageUrl(page, kb))}
            >
              {page === "library" ? "知识库与制度" : page === "wiki" ? "知识导航" : "制度问答"}
            </a>
          ))}
        </nav>
        <button
          type="button"
          className="icon-button mobile-menu"
          aria-label="打开知识库"
          aria-expanded={mobileOpen}
          onClick={() => setMobileOpen(true)}
        >
          <Icon name="menu" />
        </button>
      </header>
      <aside className="sidebar" aria-label="知识库选择">
        {!mobileOpen && picker}
        {!mobileOpen && recents}
        {storageWarning && (
          <p className="help">浏览器无法保存最近会话，请保留当前会话地址。</p>
        )}
      </aside>
      <main
        id="main"
        className={route.page === "chat" ? "chat-main" : undefined}
        tabIndex={-1}
      >
        {error !== null ? (
          <>
            <h1 id="page-title" tabIndex={-1}>
              知识库读取失败
            </h1>
            <ErrorNotice error={error} retry={refresh} />
          </>
        ) : route.page === "chat" ? (
          <ConversationView
            key={chatKey}
            conversationId={conversationId}
            kb={selected?.kb_id ?? null}
            onCreated={conversationCreated}
            onBusyChange={setChatBusy}
            onCitation={selectCitation}
            selected={
              citation
                ? { jobId: citation.jobId, number: citation.citation.number }
                : null
            }
          />
        ) : loading && !bases.length ? (
          <>
            <h1 id="page-title" tabIndex={-1}>
              {route.page === "wiki" ? "知识导航" : "知识库与制度"}
            </h1>
            <div className="source-skeleton" aria-label="正在读取知识库" />
          </>
        ) : route.page === "wiki" && selected ? (
          <WikiPage key={selected.kb_id} base={selected} />
        ) : selected ? (
          <LibraryPage
            key={selected.kb_id}
            base={selected}
            setLeaveGuard={setLeaveGuard}
            navigateToChat={() => navigate(pageUrl("chat", selected.kb_id))}
          />
        ) : (
          <>
            <div className="page-heading">
              <div>
                <p className="eyebrow">POLICY ARCHIVE</p>
                <h1 id="page-title" tabIndex={-1}>
                  {route.page === "wiki" ? "知识导航" : "知识库与制度"}
                </h1>
                <p className="muted">登记制度，跟踪处理，核对每份原件。</p>
              </div>
            </div>
            <div className="empty-state">
              <div className="empty-document">
                <Icon name="layers" />
              </div>
              <h2>
                {route.kb
                  ? "所选知识库不可用"
                  : bases.length
                    ? "选择一个知识库"
                    : "建立你的制度档案"}
              </h2>
              <p className="muted">
                {route.kb
                  ? "请重新加载列表或选择其他知识库。"
                  : bases.length
                    ? "从侧栏选择知识库，查看全部制度与原件。"
                    : "创建知识库，开始收纳和管理报销制度。"}
              </p>
              <button
                type="button"
                className="button primary"
                onClick={() => {
                  if (route.kb) refresh();
                  else if (bases.length) select(bases[0]!.kb_id);
                  else setCreating(true);
                }}
              >
                <Icon name={route.kb ? "refresh" : "plus"} />
                {route.kb
                  ? "重新加载"
                  : bases.length
                    ? "选择第一个知识库"
                    : "创建知识库"}
              </button>
            </div>
          </>
        )}
      </main>
      {route.page === "chat" && wide && citation && (
        <aside
          ref={citationPanel}
          className="citation-panel"
          aria-label="引用原文"
          onKeyDown={(event) => {
            if (event.key === "Escape") {
              event.preventDefault();
              closeCitation();
            }
          }}
        >
          <CitationPanel
            selection={citation}
            onClose={closeCitation}
          />
        </aside>
      )}
      {route.page === "chat" && !wide && citation && (
        <Dialog
          title="引用原文"
          eyebrow="PROBATIO / EVIDENCE"
          drawer
          returnFocus={citationTrigger.current}
          onClose={closeCitation}
        >
          <CitationPanel
            selection={citation}
            onClose={closeCitation}
          />
        </Dialog>
      )}
      {mobileOpen && (
        <Dialog title="知识库" drawer onClose={() => setMobileOpen(false)}>
          {picker}
          {recents}
          {storageWarning && <p className="help">浏览器无法保存最近会话，请保留当前会话地址。</p>}
        </Dialog>
      )}
      {creating && (
        <CreateKnowledgeBaseDialog
          onClose={() => setCreating(false)}
          onCreated={created}
          onRefresh={refresh}
        />
      )}
    </div>
  );
}
