// API timestamps must include a timezone. Display is always Beijing time,
// independent of the phone timezone; never infer a zone for ambiguous values.
export function beijingTimeText(value: string): string {
  if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/.test(value)) return '时间暂不可用';
  const time = Date.parse(value);
  const shifted = new Date(time + 8 * 60 * 60 * 1000);
  if (!Number.isFinite(shifted.getTime())) return '时间暂不可用';
  return shifted.toISOString().slice(0, 19).replace('T', ' ');
}
