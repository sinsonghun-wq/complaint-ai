import { useCallback, useEffect, useRef, useState } from "react";
import "./ragImport.css";

const running = (job) => ["queued", "processing"].includes(job?.status);
const terminalMessage = (job) => job.status === "cancelled"
  ? "해당 작업이 취소되었습니다. 이미 저장된 데이터는 유지됩니다."
  : job.status === "failed" ? `RAG 자료 처리에 실패했습니다. ${job.last_error || "임베딩 서버 상태를 확인해 주세요."}`
    : `RAG 자료 처리가 완료되었습니다. 저장 ${job.saved_rows.toLocaleString()}건 · 중복 ${job.duplicate_rows.toLocaleString()}건${job.failed_rows ? ` · 처리하지 못한 항목 ${job.failed_rows.toLocaleString()}건 제외` : ""}`;

export function useRagImport(api, enabled = true) {
  const [job, setJob] = useState(null);
  const [uploading, setUploading] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const [resuming, setResuming] = useState(false);
  const [message, setMessage] = useState("");
  const [failures, setFailures] = useState(null);
  const [failureError, setFailureError] = useState("");
  const [loadingFailures, setLoadingFailures] = useState(false);
  const failureLock = useRef(false);
  const uploadLock = useRef(false);
  const cancelWanted = useRef(false);
  const cancelLock = useRef(false);
  const sequence = useRef(0);
  const latestJob = useRef(null);
  const alive = useRef(true);
  const publish = useCallback((next) => {
    if (next?.id !== latestJob.current?.id) { setFailures(null); setFailureError(""); }
    latestJob.current = next; setJob(next);
  }, []);
  useEffect(() => {
    if (!enabled) return;
    alive.current = true;
    const request = ++sequence.current;
    api("/api/rag-imports/latest").then((result) => {
      if (alive.current && request === sequence.current && !uploadLock.current && result.job) {
        publish(result.job);
        if (!running(result.job)) setMessage(terminalMessage(result.job));
      }
    }).catch(() => {});
    return () => { alive.current = false; sequence.current++; };
  }, [api, enabled, publish]);
  useEffect(() => {
    if (!running(job)) return;
    let stopped = false, pending = false;
    const poll = async () => {
      if (pending || cancelLock.current) return;
      pending = true;
      const request = sequence.current;
      try {
        const next = await api(`/api/rag-imports/${job.id}`);
        if (stopped || request !== sequence.current) return;
        publish(next);
        if (!running(next)) setMessage(terminalMessage(next));
      } catch (error) { if (!stopped && request === sequence.current) setMessage(error.message); }
      finally { pending = false; }
    };
    const timer = window.setInterval(poll, 1000);
    return () => { stopped = true; window.clearInterval(timer); };
  }, [api, job?.id, job?.status, publish]);

  const cancelJob = async (target) => {
    if (!target?.id || cancelLock.current) return;
    cancelLock.current = true; sequence.current++;
    try {
      const next = await api(`/api/rag-imports/${target.id}/cancel`, { method: "POST" });
      if (!alive.current) return;
      publish(next); setMessage(next.message || terminalMessage(next));
    } catch (error) { if (alive.current) setMessage(error.message); }
    finally { cancelLock.current = false; cancelWanted.current = false; if (alive.current) setCancelling(false); }
  };
  const cancel = async () => {
    if (cancelling || (!uploadLock.current && !running(latestJob.current))) return;
    cancelWanted.current = true; setCancelling(true);
    if (latestJob.current?.id && !uploadLock.current) await cancelJob(latestJob.current);
  };
  const upload = async (file, departmentId) => {
    if (uploadLock.current || running(latestJob.current)) return;
    if (!file) return setMessage("RAG용 CSV 파일을 선택해 주세요.");
    if (!file.name.toLowerCase().endsWith(".csv")) return setMessage("파일 형식이 다릅니다. RAG용 데이터는 CSV 파일만 업로드할 수 있습니다.");
    uploadLock.current = true; cancelWanted.current = false; sequence.current++;
    setUploading(true); setCancelling(false); setMessage(""); publish(null);
    try {
      const form = new FormData(); form.append("file", file); form.append("department_id", String(departmentId));
      const next = await api("/api/rag-imports", { method: "POST", body: form });
      if (!alive.current) return;
      publish(next);
      if (cancelWanted.current) await cancelJob(next);
    } catch (error) { if (alive.current) setMessage(error.message); }
    finally { uploadLock.current = false; if (alive.current) { setUploading(false); setCancelling(false); } }
  };
  const loadFailures = async () => {
    const target = latestJob.current;
    if (failureLock.current || !target?.failed_rows || running(target)) return;
    failureLock.current = true; setLoadingFailures(true); setFailureError("");
    const request = sequence.current, offset = failures?.length || 0;
    try {
      const result = await api(`/api/rag-imports/${target.id}/failures?offset=${offset}&limit=20`);
      if (alive.current && request === sequence.current) setFailures((current) => [...(current || []), ...result.failures]);
    } catch (error) { if (alive.current && request === sequence.current) setFailureError(error.message); }
    finally { failureLock.current = false; if (alive.current) setLoadingFailures(false); }
  };
  const resume = async () => {
    const target = latestJob.current;
    if (uploadLock.current || running(target) || !target?.resumable_rows) return;
    uploadLock.current = true; cancelWanted.current = false; sequence.current++; setResuming(true); setMessage("");
    try {
      const next = await api(`/api/rag-imports/${target.id}/resume`, { method: "POST" });
      if (!alive.current) return;
      publish(next); setFailures(null); setFailureError("");
      if (cancelWanted.current) await cancelJob(next);
    } catch (error) { if (alive.current) setMessage(error.message); }
    finally { uploadLock.current = false; if (alive.current) { setResuming(false); setCancelling(false); } }
  };
  return { job, uploading, cancelling, resuming, message, busy: uploading || resuming || running(job), upload, cancel, resume, failures, failureError, loadingFailures, loadFailures };
}

function RagImportFailures({ importer }) {
  const { job, failures, failureError, loadingFailures, loadFailures } = importer;
  if (!job?.failed_rows || running(job)) return null;
  return <details className="rag-import-failures" key={job.id} onToggle={(event) => {
    if (event.currentTarget.open && failures === null && !loadingFailures) loadFailures();
  }}>
    <summary>처리하지 못한 항목 {job.failed_rows.toLocaleString()}건</summary>
    <p>아래 항목은 RAG 검색용 처리를 완료하지 못했습니다. 긴 원문과 저장된 조각은 보존됩니다. 행 번호는 CSV 헤더를 포함합니다.</p>
    {failures?.length > 0 && <ul>{failures.map((item) => <li key={item.source_row}><strong>CSV {item.source_row.toLocaleString()}행</strong>: {item.reason}</li>)}</ul>}
    {loadingFailures && <p role="status">제외 항목을 불러오는 중...</p>}
    {failureError && <p role="alert">{failureError}</p>}
    {!loadingFailures && (failureError || (failures && failures.length < job.failed_rows)) && <button onClick={loadFailures}>{failureError ? "다시 불러오기" : "다음 20건 보기"}</button>}
  </details>;
}

export function RagImportPanel({ importer, departments, ownDepartmentId }) {
  const [file, setFile] = useState(null);
  const [department, setDepartment] = useState(ownDepartmentId || 7);
  useEffect(() => { setDepartment(ownDepartmentId || 7); }, [ownDepartmentId]);
  return <section className="panel compose rag-import-panel" aria-labelledby="rag-import-title">
    <h2 id="rag-import-title">RAG용 데이터 삽입</h2>
    <p className="muted">법률·규칙·명확한 사례를 AI 민원 답변의 참고 자료로 저장합니다. 본문이 완전히 같은 자료는 건너뜁니다.</p>
    <p className="muted">CSV의 ‘제목·본문’ 또는 ‘title·content’ 열을 사용하세요. 사례는 ‘질문·답변’ 열도 지원합니다. 본문은 그대로 보존하고 현재 임베딩 모델로 검색 벡터를 생성합니다.</p>
    {importer.message && <p className="notice" role={importer.job?.status === "failed" ? "alert" : "status"}>{importer.message}</p>}
    <RagImportFailures importer={importer} />
    <label>자료 부서<select disabled={importer.busy} value={department} onChange={(event) => setDepartment(Number(event.target.value))}>{departments.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label>
    <label className="upload">RAG CSV 파일 선택<input type="file" accept=".csv,text/csv" disabled={importer.busy} onChange={(event) => setFile(event.target.files?.[0] || null)} /><small>{file?.name || "CSV 파일만 지원"}</small></label>
    <div className="actions compose-actions"><button className="primary" disabled={importer.busy} onClick={() => importer.upload(file, department)}>{importer.busy ? "RAG 자료 처리 중..." : "RAG 데이터 처리 시작"}</button>{importer.busy && <button className="danger rag-cancel" disabled={importer.cancelling} onClick={importer.cancel}>{importer.cancelling ? "취소 중..." : "RAG 작업 취소"}</button>}</div>
    <p className="muted">100건마다 저장을 확정하고 진행률에 반영합니다. 취소하면 이미 저장된 데이터는 유지됩니다.</p>
    {!importer.busy && importer.job?.resumable_rows > 0 && <button className="primary" onClick={importer.resume}>긴 원문 이어서 처리</button>}
    <p className="muted">8,192토큰 초과 원문은 후순위로 보관합니다. 짧은 자료를 먼저 처리한 뒤 긴 원문을 조각으로 저장하며, 모든 조각이 완료된 원문만 검색에 사용합니다.</p>
  </section>;
}

export function RagImportProgress({ importer }) {
  const { job, uploading, cancelling, cancel } = importer;
  if (!job && !uploading) return null;
  const completed = job?.completed_rows || 0, total = job?.total_rows || 0;
  const percentage = job?.status === "completed" ? 100 : total ? Math.min(99, Math.floor(completed / total * 100)) : 0;
  const status = uploading ? "파일 업로드 중" : job?.stage === "counting" ? "전체 건수 확인 중" : job?.stage === "chunking" && running(job) ? (job.status === "queued" ? "긴 원문 후순위 대기" : "긴 원문 조각 처리 중") : ({ queued: "대기 중", processing: "임베딩·저장 중", completed: "처리 완료", cancelled: "작업 취소됨", failed: "처리 실패" }[job?.status] || "준비 중");
  return <section className={`import-progress rag-progress ${job?.status || ""}`} aria-label="RAG 파일 처리 진행률">
    <div className="import-progress-heading"><span>RAG 파일 처리</span><strong>{percentage}%</strong></div>
    <small>{status}</small>
    <div className="import-progress-track" role="progressbar" aria-label="RAG 파일 처리" aria-valuemin={0} aria-valuemax={100} aria-valuenow={percentage}><div className="import-progress-fill" style={{ width: `${percentage}%` }} /></div>
    <small className="import-progress-file" title={job?.source_file}>{job?.source_file || "CSV 업로드"}</small>
    <small>{completed.toLocaleString()} / {total ? `${total.toLocaleString()}건 확정 처리` : "전체 건수 확인 중"}</small>
    {job && <small>저장 {job.saved_rows.toLocaleString()} · 중복 {job.duplicate_rows.toLocaleString()}{job.failed_rows ? ` · 제외 ${job.failed_rows.toLocaleString()}` : ""}</small>}
    {job?.deferred_rows > 0 && <small>긴 원문 후순위 {job.deferred_rows.toLocaleString()}건 · 미처리 {Math.max(0, job.deferred_rows - (job.long_processed_rows || 0)).toLocaleString()}건</small>}
    {job?.active_source_row && job?.active_chunks_total > 0 && <small>CSV {job.active_source_row.toLocaleString()}행 · 조각 {job.active_chunks_completed.toLocaleString()} / {job.active_chunks_total.toLocaleString()}개 저장</small>}
    {importer.busy && <button className="danger rag-cancel" disabled={cancelling} onClick={cancel}>{cancelling ? "취소 중..." : "RAG 작업 취소"}</button>}
    {!importer.busy && job?.resumable_rows > 0 && <button className="rag-resume" onClick={importer.resume}>긴 원문 이어서 처리</button>}
    {job?.status === "cancelled" && <small role="status">해당 작업이 취소되었습니다.</small>}
    {job?.status === "failed" && <small role="alert">{job.last_error}</small>}
  </section>;
}
