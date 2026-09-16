/** Restore a failed submission without discarding text typed while awaiting it. */
export function recoverDraft(current: string, submitted: string): string {
  if (!current) return submitted;
  if (!submitted) return current;
  return `${submitted}\n\n${current}`;
}

export function recoverAttachments(current: string[], submitted: string[]): string[] {
  return [...new Set([...submitted, ...current])];
}
