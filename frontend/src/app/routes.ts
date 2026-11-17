export type Route =
  | { page: "library"; kb: string | null }
  | { page: "wiki"; kb: string | null }
  | { page: "chat"; kb: string | null; conversationId: string | null };
export function readRoute(): Route {
  const url = new URL(window.location.href);
  if (/^\/wiki\/?$/.test(url.pathname))
    return { page: "wiki", kb: url.searchParams.get("kb") };
  const match = /^\/chat(?:\/([^/]+))?\/?$/.exec(url.pathname);
  return match
    ? {
        page: "chat",
        kb: url.searchParams.get("kb"),
        conversationId: match[1] ?? null,
      }
    : { page: "library", kb: url.searchParams.get("kb") };
}
export const pageUrl = (page: "library" | "chat" | "wiki", kb: string | null) =>
  `/${page}${kb ? `?kb=${encodeURIComponent(kb)}` : ""}`;
export const conversationUrl = (id: string, kb: string | null) =>
  `/chat/${encodeURIComponent(id)}${kb ? `?kb=${encodeURIComponent(kb)}` : ""}`;
