import { useEffect, useId, useState } from "react";
import departments from "../../app/departments.json";
import "./complaintFilters.css";

export const departmentLabels = departments.map((department) => department.name);
export const complaintViewStatuses = [
  { value: "unassigned", label: "미접수" },
  { value: "in_progress", label: "접수 중" },
  { value: "completed", label: "접수 완료" },
];

export function citizenViewStatusLabel(record) {
  if (record?.complaint_status === "취소" || record?.deleted_at) return "취소";
  if (!String(record?.category || "").trim()) return "미접수";
  return record?.latest_response_state === "sent" ? "접수 완료" : "접수 중";
}

export function normalizeComplaintFilters(value) {
  return {
    departments: departmentLabels.filter((name) => Array.isArray(value?.departments) && value.departments.includes(name)),
    view_status: complaintViewStatuses.some(({ value: status }) => status === value?.view_status) ? value.view_status : "",
  };
}

export function appendComplaintFilters(params, filters) {
  const normalized = normalizeComplaintFilters(filters);
  params.delete("departments"); params.delete("view_status");
  normalized.departments.forEach((department) => params.append("departments", department));
  if (normalized.view_status) params.set("view_status", normalized.view_status);
  return params;
}

export default function ComplaintFilters({ initialValue, onApply, onClear, busy = false, compact = false }) {
  const id = useId();
  const [draft, setDraft] = useState(() => normalizeComplaintFilters(initialValue));
  const initialKey = JSON.stringify(normalizeComplaintFilters(initialValue));
  useEffect(() => { setDraft(JSON.parse(initialKey)); }, [initialKey]);
  const valid = draft.departments.length > 0 && Boolean(draft.view_status);
  const submit = (event) => {
    event.preventDefault();
    if (!busy && valid) onApply?.(normalizeComplaintFilters(draft));
  };
  const clear = () => {
    if (busy) return;
    setDraft(normalizeComplaintFilters(null)); onClear?.();
  };
  return <section className={`complaint-filters${compact ? " complaint-filters-compact" : ""}`} aria-label="민원 조회 조건">
    {!compact && <h3>어떤 민원들을 조회하시겠습니까?</h3>}
    <form onSubmit={submit}>
      <fieldset disabled={busy}><legend>부서 선택</legend><p>한 개 이상 선택해 주세요. 선택한 부서 중 하나에 해당하면 표시합니다.</p><div className="complaint-filters-departments">{departmentLabels.map((department, index) => <label key={department} htmlFor={`${id}-department-${index}`}><input id={`${id}-department-${index}`} type="checkbox" checked={draft.departments.includes(department)} onChange={(event) => setDraft((current) => ({ ...current, departments: event.target.checked ? [...current.departments, department] : current.departments.filter((name) => name !== department) }))} /><span>{department}</span></label>)}</div></fieldset>
      <fieldset disabled={busy}><legend>처리 상태</legend><div className="complaint-filters-statuses">{complaintViewStatuses.map(({ value, label }) => <label key={value} htmlFor={`${id}-${value}`}><input id={`${id}-${value}`} type="radio" name={`${id}-view-status`} value={value} checked={draft.view_status === value} onChange={() => setDraft((current) => ({ ...current, view_status: value }))} /><span>{label}</span></label>)}</div></fieldset>
      <p className="complaint-filters-help">미접수를 선택하면 선택한 부서와 관계없이 본인의 부서 미정 민원을 모두 조회합니다. 접수 중은 담당 부서가 정해졌으며 공식 답변이 아직 전달되지 않은 민원입니다.</p>
      <div className="complaint-filters-actions"><button type="submit" disabled={busy || !valid}>조회하기</button><button type="button" disabled={busy} onClick={clear}>조건 초기화</button></div>
    </form>
  </section>;
}
