export function importPercentage(progress) {
  if (progress?.status === "completed") return 100;
  const total = Number(progress?.total);
  const completed = Number(progress?.completed);
  if (!Number.isFinite(total) || total <= 0 || !Number.isFinite(completed)) return 0;
  return Math.max(0, Math.min(99, Math.floor(completed / total * 100)));
}

export function csvUnreadableNotice(count) {
  const unreadable = Number(count);
  return Number.isInteger(unreadable) && unreadable > 0
    ? `필드 길이가 200만 자를 초과하여 읽을 수 없는 CSV 항목 ${unreadable.toLocaleString()}건은 제외했습니다.`
    : "";
}

export function csvCompletionMessage(state) {
  return [`CSV 처리가 완료되었습니다. 총 ${state.saved_rows.toLocaleString()}건의 민원이 요약 및 분류 되었습니다.`,
    csvUnreadableNotice(state.unreadable_rows)].filter(Boolean).join(" ");
}
