const key = (kind: string, id: string) => `probatio:cursor:${kind}:${id}`;
export function readCursor(kind: string, id: string, unavailable?: () => void): number {
  try {
    const raw = sessionStorage.getItem(key(kind, id));
    if (raw === null || !/^\d+$/.test(raw)) return 0;
    const value = Number(raw);
    return Number.isSafeInteger(value) ? value : 0;
  } catch {
    unavailable?.();
    return 0;
  }
}
export function writeCursor(
  kind: string,
  id: string,
  sequence: number,
): boolean {
  try {
    sessionStorage.setItem(key(kind, id), String(sequence));
    return true;
  } catch {
    return false;
  }
}
