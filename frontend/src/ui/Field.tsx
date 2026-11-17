import { useEffect, useRef, type ReactNode } from "react";
export function Field({
  id,
  label,
  error,
  hint,
  children,
}: {
  id: string;
  label: string;
  error?: string;
  hint?: string;
  children: ReactNode;
}) {
  return (
    <div className="field">
      <label htmlFor={id}>{label}</label>
      {children}
      {hint && (
        <p className="help" id={`${id}-hint`}>
          {hint}
        </p>
      )}
      {error && (
        <p className="field-error" id={`${id}-error`}>
          {error}
        </p>
      )}
    </div>
  );
}
export function FieldErrors({ errors }: { errors: Record<string, string> }) {
  const ref = useRef<HTMLDivElement>(null);
  const entries = Object.entries(errors);
  useEffect(() => {
    const names = Object.keys(errors);
    if (names.length > 1) ref.current?.focus();
    else if (names[0]) document.getElementById(names[0])?.focus();
  }, [errors]);
  if (entries.length < 2) return null;
  return (
    <div className="notice danger" tabIndex={-1} ref={ref}>
      <p>请修正以下字段：</p>
      <ul>
        {entries.map(([id, text]) => (
          <li key={id}>
            <a
              href={`#${id}`}
              onClick={(event) => {
                event.preventDefault();
                document.getElementById(id)?.focus();
              }}
            >
              {text}
            </a>
          </li>
        ))}
      </ul>
    </div>
  );
}
