import { useEffect, useRef } from "react";
import "./DepartmentArchiveDialog.css";

export default function DepartmentArchiveDialog({ target, reason, returnFocus, busy, error, onConfirm, onCancel }) {
  const dialog = useRef(null);
  useEffect(() => {
    const overflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => { document.body.style.overflow = overflow; returnFocus?.focus?.(); };
  }, [returnFocus]);
  useEffect(() => {
    const keyboard = (event) => {
      if (event.key === "Escape" && !busy) { event.preventDefault(); onCancel(); }
      if (event.key === "Tab") {
        const controls = [...(dialog.current?.querySelectorAll("button:not(:disabled)") || [])];
        const first = controls[0], last = controls.at(-1);
        if (!first) { event.preventDefault(); return; }
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
      }
    };
    document.addEventListener("keydown", keyboard);
    return () => document.removeEventListener("keydown", keyboard);
  }, [busy, onCancel]);

  return <div className="trash-modal-backdrop"><section ref={dialog} className="trash-modal department-archive-modal" role="dialog" aria-modal="true" aria-labelledby="department-archive-title" aria-describedby="department-archive-warning" aria-busy={busy}>
    <h2 id="department-archive-title">부서 민원 전체 삭제 확인</h2>
    <p id="department-archive-warning">{target.name} 부서의 전체 민원을 삭제합니다. 출처 필터와 관계없이 적용됩니다. 정말로 전체 삭제하시겠습니까?</p>
    <p>접수 대기·접수 됨·완료·취소 상태와 파일 업로드·민원인 작성 여부, 현재 페이지에 관계없이 부서 민원 {Number.isFinite(target.count) ? `전체 ${target.count.toLocaleString()}건을` : "전체를"} <b>삭제된 데이터 보관함</b>으로 이동합니다. DB의 원본은 유지됩니다.</p>
    <p className="muted">기본 삭제 사유: {reason}</p>
    {error && <p className="notice" role="alert">{error}</p>}
    {busy && <p role="status">삭제된 데이터로 이동 중입니다.</p>}
    <div className="actions"><button autoFocus disabled={busy} onClick={onCancel}>취소</button><button className="danger" disabled={busy} onClick={onConfirm}>{busy ? "이동 중..." : "삭제된 데이터로 이동"}</button></div>
  </section></div>;
}
