export function importPercentage(progress) {
  if (progress?.status === "completed") return 100;
  const total = Number(progress?.total);
  const completed = Number(progress?.completed);
  if (!Number.isFinite(total) || total <= 0 || !Number.isFinite(completed)) return 0;
  return Math.max(0, Math.min(99, Math.floor(completed / total * 100)));
}
