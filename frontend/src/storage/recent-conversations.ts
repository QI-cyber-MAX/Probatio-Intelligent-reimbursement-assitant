export interface RecentConversation {
  conversation_id: string;
  kb_id: string | null;
  last_opened_at: string;
}
const key = "probatio:recent-conversations";
export function readRecentConversations(unavailable?: () => void): RecentConversation[] {
  try {
    const values: unknown = JSON.parse(localStorage.getItem(key) ?? "[]");
    if (!Array.isArray(values)) return [];
    return values
      .filter(
        (v): v is RecentConversation =>
          v &&
          typeof v === "object" &&
          typeof v.conversation_id === "string" &&
          (v.kb_id === null || typeof v.kb_id === "string") &&
          typeof v.last_opened_at === "string" &&
          Number.isFinite(Date.parse(v.last_opened_at)),
      )
      .sort((a, b) => b.last_opened_at.localeCompare(a.last_opened_at))
      .slice(0, 20);
  } catch {
    unavailable?.();
    return [];
  }
}
export function saveRecentConversations(values: RecentConversation[]): boolean {
  try {
    localStorage.setItem(key, JSON.stringify(values.slice(0, 20)));
    return true;
  } catch {
    return false;
  }
}
