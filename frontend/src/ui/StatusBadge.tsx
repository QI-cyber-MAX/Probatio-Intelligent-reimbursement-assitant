import type { ReactNode } from "react";
export function StatusBadge({
  tone = "neutral",
  children,
}: {
  tone?: "neutral" | "success" | "warning" | "danger";
  children: ReactNode;
}) {
  return (
    <span className={`badge ${tone}`}>
      <span className="status-dot" aria-hidden="true" />
      {children}
    </span>
  );
}
