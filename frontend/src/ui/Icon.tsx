export type IconName =
  | "document"
  | "plus"
  | "arrow"
  | "upload"
  | "close"
  | "check"
  | "clock"
  | "warning"
  | "refresh"
  | "menu"
  | "layers";
const paths: Record<IconName, string> = {
  document: "M5 2h9l5 5v15H5z M14 2v6h5 M9 12h6 M9 16h6",
  plus: "M12 5v14 M5 12h14",
  arrow: "M4 12h16 M14 6l6 6-6 6",
  upload: "M12 16V3 M6 9l6-6 6 6 M4 16v5h16v-5",
  close: "m6 6 12 12 M6 18 18 6",
  check: "m5 12 4 4L19 6",
  clock: "M12 8v5l3 2 M22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0",
  warning: "M12 8v5 M12 16v1 M12 3 2 21h20z",
  refresh: "M20 8a9 9 0 1 0 0 8 M20 2v6h-6",
  menu: "M3 6h18 M3 12h18 M3 18h18",
  layers: "m12 2 10 6-10 6L2 8z M2 12l10 6 10-6 M2 16l10 6 10-6",
};
export function Icon({
  name,
  className = "",
}: {
  name: IconName;
  className?: string;
}) {
  return (
    <svg
      className={`icon ${className}`}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d={paths[name]} />
    </svg>
  );
}
