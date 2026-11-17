import type { RecentConversation } from "../storage/recent-conversations";
import { Icon } from "../ui/Icon";
import { conversationUrl } from "./routes";
export function RecentConversations({
  values,
  current,
  onOpen,
  onRemove,
  onNew,
}: {
  values: RecentConversation[];
  current: string | null;
  onOpen(id: string, kb: string | null): void;
  onRemove(id: string): void;
  onNew(): void;
}) {
  return (
    <section className="recent-conversations" aria-label="最近会话">
      <button className="button secondary full" onClick={onNew}>
        <Icon name="plus" />
        新会话
      </button>
      <ul>
        {values.map((value) => (
          <li key={value.conversation_id}>
            <a
              href={conversationUrl(value.conversation_id, value.kb_id)}
              aria-current={
                current === value.conversation_id ? "page" : undefined
              }
              onClick={(event) => {
                if (
                  event.button ||
                  event.ctrlKey ||
                  event.metaKey ||
                  event.shiftKey ||
                  event.altKey
                )
                  return;
                event.preventDefault();
                onOpen(value.conversation_id, value.kb_id);
              }}
            >
              {new Date(value.last_opened_at).toLocaleDateString("zh-CN", {
                month: "2-digit",
                day: "2-digit",
              })}{" "}
              · {value.conversation_id.slice(0, 8)}
            </a>
            <button
              className="icon-button"
              aria-label={`从最近会话移除 ${value.conversation_id.slice(0, 8)}`}
              onClick={() => onRemove(value.conversation_id)}
            >
              <Icon name="close" />
            </button>
          </li>
        ))}
      </ul>
    </section>
  );
}
