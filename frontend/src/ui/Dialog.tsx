import { useEffect, useRef, type ReactNode } from "react";
import { Icon } from "./Icon";

export function Dialog({
  title,
  children,
  onClose,
  drawer = false,
  busy = false,
  returnFocus,
  eyebrow,
}: {
  title: string;
  children: ReactNode;
  onClose(): void;
  drawer?: boolean;
  busy?: boolean;
  returnFocus?: HTMLElement | null;
  eyebrow?: string;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const restoreFocus = useRef(returnFocus);
  useEffect(() => {
    const trigger = restoreFocus.current ?? document.activeElement;
    const dialog = ref.current!;
    dialog.showModal();
    return () => {
      dialog.close();
      if (trigger instanceof HTMLElement && trigger.isConnected)
        trigger.focus();
    };
  }, []);
  return (
    <dialog
      ref={ref}
      className={drawer ? "dialog drawer" : "dialog"}
      aria-labelledby="dialog-title"
      onCancel={(event) => {
        event.preventDefault();
        if (!busy) onClose();
      }}
      onClick={(event) => {
        if (event.target === event.currentTarget && !busy) onClose();
      }}
    >
      <div className="dialog-content">
        <header className="dialog-heading">
          <div>
            <p className="eyebrow">
              {eyebrow ??
                `PROBATIO / ${drawer ? "POLICY REGISTRATION" : "KNOWLEDGE BASE"}`}
            </p>
            <h2 id="dialog-title">{title}</h2>
          </div>
          <button
            type="button"
            className="icon-button"
            aria-label="关闭"
            disabled={busy}
            onClick={onClose}
          >
            <Icon name="close" />
          </button>
        </header>
        {children}
      </div>
    </dialog>
  );
}
