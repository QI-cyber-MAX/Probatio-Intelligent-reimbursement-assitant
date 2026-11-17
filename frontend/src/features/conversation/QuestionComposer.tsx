import { useRef, type FormEvent } from "react";
import { Icon } from "../../ui/Icon";
export function QuestionComposer({
  value,
  onChange,
  onSend,
  disabled,
  sending,
  readOnly,
  hint,
  error,
}: {
  value: string;
  onChange(value: string): void;
  onSend(): void;
  disabled: boolean;
  sending: boolean;
  readOnly: boolean;
  hint: string | null;
  error: string | null;
}) {
  const composing = useRef(false);
  const input = useRef<HTMLTextAreaElement>(null);
  function submit(event?: FormEvent) {
    event?.preventDefault();
    if (disabled) return;
    input.current?.focus();
    onSend();
  }
  return (
    <form className="question-composer" onSubmit={submit}>
      <label className="sr-only" htmlFor="question">
        制度问题
      </label>
      <div className="composer-controls">
        <textarea
          id="question"
          ref={input}
          rows={2}
          value={value}
          readOnly={readOnly}
          placeholder="输入制度问题，例如：出差住宿报销有哪些条件？"
          aria-invalid={!!error}
          aria-describedby={
            [hint && "composer-hint", error && "question-error"]
              .filter(Boolean)
              .join(" ") || undefined
          }
          title="Enter 发送，Shift+Enter 换行"
          onChange={(event) => onChange(event.target.value)}
          onCompositionStart={() => {
            composing.current = true;
          }}
          onCompositionEnd={() => {
            composing.current = false;
          }}
          onKeyDown={(event) => {
            if (
              event.key === "Enter" &&
              !event.shiftKey &&
              !event.nativeEvent.isComposing &&
              !composing.current &&
              event.keyCode !== 229
            ) {
              event.preventDefault();
              submit();
            }
          }}
        />
        <button className="button primary" type="submit" disabled={disabled}>
          {sending ? "发送中…" : "发送"}
          <Icon name="arrow" />
        </button>
      </div>
      {error && (
        <p className="field-error" id="question-error" role="alert">
          {error}
        </p>
      )}
      {hint && (
        <p className="help" id="composer-hint">{hint}</p>
      )}
    </form>
  );
}
