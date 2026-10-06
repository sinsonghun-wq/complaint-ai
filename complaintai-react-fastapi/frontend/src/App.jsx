import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { importPercentage } from "./importProgress.js";
import departments from "../../app/departments.json";

const categories = departments.map((department) => department.name);
const statusLabel = (value) => ({ "접수": "접수 대기", "진행중": "접수 됨" }[value] || value);
const isOpen = (record) => ["접수", "진행중"].includes(record?.complaint_status);
const key = "complaintai.fastapi.auth";

export default function App() {
  const [auth, setAuth] = useState(() => JSON.parse(sessionStorage.getItem(key) || "null"));
  const [server, setServer] = useState(localStorage.getItem("complaintai.fastapi.server") || window.location.origin);
  const headers = useMemo(() => auth?.token ? { Authorization: `Bearer ${auth.token}` } : {}, [auth]);
  const api = useCallback(async (path, options = {}) => {
    const response = await fetch(`${server.replace(/\/$/, "")}${path}`, { ...options, headers: { ...headers, ...options.headers } });
    const raw = await response.text();
    let data = {};
    try { data = raw ? JSON.parse(raw) : {}; } catch {
      throw new Error(response.ok ? "처리 서버가 올바른 결과를 반환하지 않았습니다. 서버 주소를 확인해 주세요." : `처리 서버 응답을 읽지 못했습니다. (HTTP ${response.status})`);
    }
    if (response.status === 401 && auth?.token) {
      // Removed administrator accounts must not retain a stale department UI.
      sessionStorage.removeItem(key); setAuth(null);
    }
    if (!response.ok) throw new Error(data.detail || data.message || "요청을 처리하지 못했습니다.");
    return data;
  }, [headers, server]);
  const saveAuth = (data) => { sessionStorage.setItem(key, JSON.stringify(data)); setAuth(data); };
  if (!auth) return <LoginScreen api={api} saveAuth={saveAuth} server={server} setServer={setServer} />;
  return <Workspace auth={auth} api={api} signout={() => { sessionStorage.removeItem(key); setAuth(null); }} server={server} setServer={setServer} />;
}

function LoginScreen({ api, saveAuth, server, setServer }) {
  const [mode, setMode] = useState("login");
  const [message, setMessage] = useState("");
  const submit = async (event) => {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    try {
      const body = { username: form.get("username"), password: form.get("password"), ...(mode === "signup" ? { display_name: form.get("display_name") } : {}) };
      saveAuth(await api(`/api/auth/${mode === "login" ? "login" : "signup"}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }));
    } catch (error) { setMessage(error.message); }
  };
  return <main className="login-screen"><section className="login-card">
    <div className="brand login-brand"><b>C</b><strong>ComplaintAI</strong></div>
    <p className="eyebrow">민원 처리 시스템</p>
    <h1>{mode === "login" ? "로그인" : "일반 사용자 회원가입"}</h1>
    <p className="muted">로그인 후 계정 권한에 맞는 민원 업무를 이용할 수 있습니다.</p>
    {message && <p className="notice">{message}</p>}
    <form onSubmit={submit} className="auth">
      {mode === "signup" && <label>계정 이름<input name="display_name" required /></label>}
      <label>ID<input name="username" required autoComplete="username" /></label>
      <label>비밀번호<input name="password" type="password" minLength="8" required autoComplete={mode === "login" ? "current-password" : "new-password"} /></label>
      <button className="primary">{mode === "login" ? "로그인" : "회원가입"}</button>
    </form>
    <button className="link-button" onClick={() => { setMode(mode === "login" ? "signup" : "login"); setMessage(""); }}>{mode === "login" ? "일반 사용자 회원가입" : "로그인으로 돌아가기"}</button>
    <label className="server-setting">처리 서버 주소<input value={server} onChange={(e) => { setServer(e.target.value); localStorage.setItem("complaintai.fastapi.server", e.target.value); }} /></label>
    <small>개인정보는 요약 결과에 노출되지 않도록 제외해 처리합니다.</small>
  </section></main>;
}

function Workspace({ auth, api, signout, server, setServer }) {
  const isAdmin = auth.user.role === "admin";
  const [view, setView] = useState(isAdmin ? "upload" : "write");
  const [message, setMessage] = useState("");
  const [title, setTitle] = useState("");
  const [content, setContent] = useState("");
  const [analysis, setAnalysis] = useState(null);
  const [editing, setEditing] = useState(null);
  const [followUp, setFollowUp] = useState(null);
  const [actionBusy, setActionBusy] = useState(false);
  const [file, setFile] = useState(null);
  const [job, setJob] = useState(null);
  const [csvMapping, setCsvMapping] = useState(null);
  const [isUploading, setIsUploading] = useState(false);
  const [importProgress, setImportProgress] = useState(null);
  const [counts, setCounts] = useState({});
  const [active, setActive] = useState(auth.user.department || "기타");
  const [records, setRecords] = useState([]);
  const [total, setTotal] = useState(0);
  const [pageSize, setPageSize] = useState(10);
  const [page, setPage] = useState(1);
  const [sourceFilter, setSourceFilter] = useState("all");
  const [selected, setSelected] = useState(null);
  const [responseText, setResponseText] = useState("");
  const [transferTarget, setTransferTarget] = useState("");
  const [deletingScope, setDeletingScope] = useState(null);
  const importTimer = useRef(null);
  const listRequest = useRef(0);
  useEffect(() => () => window.clearInterval(importTimer.current), []);

  const refresh = useCallback(async () => {
    const data = await api("/api/complaints/counts");
    setCounts(Object.fromEntries(data.categories.map((item) => [item.category, item.count])));
  }, [api]);
  const load = useCallback(async () => {
    const requestId = ++listRequest.current;
    const path = isAdmin
      ? `/api/complaints?deleted=${view === "deleted"}&category=${encodeURIComponent(view === "deleted" ? "" : active)}&source=${sourceFilter}&limit=${pageSize}&offset=${(page - 1) * pageSize}`
      : `/api/complaints?deleted=false&limit=${pageSize}&offset=${(page - 1) * pageSize}`;
    const [data, countData] = await Promise.all([api(path), api("/api/complaints/counts")]);
    if (requestId !== listRequest.current) return;
    setRecords(data.complaints); setTotal(data.total);
    setCounts(Object.fromEntries(countData.categories.map((item) => [item.category, item.count])));
  }, [active, api, isAdmin, page, pageSize, sourceFilter, view]);
  useEffect(() => { refresh().catch((error) => setMessage(error.message)); }, [refresh]);
  useEffect(() => {
    if (view === "categories" || view === "deleted" || view === "submitted") load().catch((error) => setMessage(error.message));
  }, [load, view]);
  useEffect(() => {
    if (view !== "submitted" && view !== "categories" && view !== "deleted") return;
    let stopped = false;
    const timer = window.setInterval(() => {
      if (!stopped) load().catch((error) => setMessage(error.message));
    }, 3000);
    return () => { stopped = true; window.clearInterval(timer); };
  }, [load, view]);
  useEffect(() => {
    const current = view === "intake" ? selected : view === "write" ? editing : null;
    if (!current) return;
    let stopped = false;
    let pending = false;
    const check = async () => {
      if (pending) return;
      pending = true;
      try {
        const data = await api(`/api/complaints/${current.id}`);
        if (stopped) return;
        if (view === "intake") {
          setSelected(data.complaint);
          if (data.complaint.complaint_status === "취소" || data.complaint.deleted_at) setResponseText("");
        } else if (data.complaint.complaint_status !== "접수" || data.complaint.deleted_at) {
          setEditing(null); setAnalysis(null); setContent(""); setTitle(""); setView("submitted");
          setMessage("관리자가 민원 접수를 시작했거나 민원이 취소되어 더 이상 수정할 수 없습니다.");
        }
      } catch (error) { if (!stopped) setMessage(error.message); }
      finally { pending = false; }
    };
    const timer = window.setInterval(check, 2000);
    return () => { stopped = true; window.clearInterval(timer); };
  }, [api, editing?.id, selected?.id, view]);
  const navigate = (next, keepMessage = false) => { setView(next); if (!keepMessage) setMessage(""); };

  const summarize = async () => {
    await submitComplaint();
  };
  const submitComplaint = async () => {
    if (actionBusy) return;
    if (!content.trim()) { setMessage("민원 내용을 입력해 주세요."); return; }
    setActionBusy(true);
    try {
      setMessage("");
      let receiptMessage = "민원이 접수되었습니다.";
      if (editing) {
        await api(`/api/complaints/${editing.id}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ title, content }) });
      } else if (followUp) {
        await api(`/api/complaints/${followUp.id}/follow-up`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ title, content }) });
      } else {
        const result = await api("/api/complaints", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ title, content }) });
        receiptMessage = result.message || receiptMessage;
      }
      setPage(1); setTitle(""); setContent(""); setAnalysis(null); setEditing(null); setFollowUp(null);
      setMessage(editing ? "민원이 수정되었습니다." : followUp ? "이전 민원과 답변을 포함한 재민원이 접수되었습니다." : receiptMessage); navigate("submitted", true);
    } catch (error) { setMessage(error.message); } finally { setActionBusy(false); }
  };
  const edit = (record) => { if (record.complaint_status !== "접수") return; setFollowUp(null); setEditing(record); setTitle(record.title); setContent(record.content); setAnalysis(null); navigate("write"); };
  const writeFollowUp = (record) => { setEditing(null); setFollowUp(record); setTitle(`재민원: ${record.title}`); setContent(""); setAnalysis(null); navigate("write"); };
  const remove = async (id, reason = "") => {
    if (!isAdmin && !window.confirm("이 민원을 삭제할까요?")) return false;
    if (isAdmin && !reason.trim()) return false;
    try { const result = await api(`/api/complaints/${id}`, { method: "DELETE", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ reason: reason.trim() }) }); await refresh(); await load(); setMessage(result.message); return true; } catch (error) { setMessage(error.message); return false; }
  };
  const removeCategory = async () => {
    if (!active || total === 0) return;
    if (!window.confirm(`${active} 부서의 전체 민원을 삭제합니다. 출처 필터와 관계없이 적용됩니다. 정말로 전체 삭제하시겠습니까?`)) return;
    const reason = window.prompt("접수 대기·접수 됨 상태의 민원에 적용할 취소 사유를 입력하세요. 완료·취소 민원은 제외됩니다.");
    if (!reason?.trim()) return;
    setDeletingScope("category");
    try {
      const result = await api(`/api/complaints/category/${encodeURIComponent(active)}`, { method: "DELETE", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ reason }) });
      setPage(1); await refresh(); await load();
      setMessage(`${active} 부서 민원 ${result.deleted.toLocaleString()}건을 취소했습니다.`);
    } catch (error) { setMessage(error.message); } finally { setDeletingScope(null); }
  };
  const restore = async (id) => { try { await api(`/api/complaints/${id}/restore`, { method: "POST" }); await refresh(); await load(); } catch (error) { setMessage(error.message); } };
  const hardDelete = async (id) => {
    const all = id === "all";
    if (deletingScope) return;
    if (!window.confirm(all ? "모든 부서의 삭제된 데이터를 전부 영구 삭제합니다. 현재 출처 필터와 페이지에 관계없이 적용되며 복구할 수 없습니다. 계속하시겠습니까?" : "이 데이터를 영구 삭제하면 복구할 수 없습니다. 계속하시겠습니까?")) return;
    const password = window.prompt("로그인 비밀번호를 입력하세요."); if (!password) return;
    setDeletingScope("permanent");
    try {
      const result = await api(all ? "/api/complaints/deleted/all" : `/api/complaints/${id}/permanent`, { method: "DELETE", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ password }) });
      setPage(1); await refresh(); await load();
      setMessage(`${result.permanently_deleted.toLocaleString()}건을 영구 삭제했습니다.`);
    } catch (error) { setMessage(error.message); } finally { setDeletingScope(null); }
  };
  const deleteDepartmentAll = async () => {
    if (!isAdmin) return;
    if (!window.confirm("테스트 전용 기능입니다. 모든 부서의 민원(완료·취소 포함)을 취소 상태로 바꾸고 삭제된 데이터로 이동합니다. 실제 데이터는 영구 삭제하지 않습니다. 계속하시겠습니까?")) return;
    setDeletingScope("all");
    try {
      const result = await api("/api/complaints/department/all", { method: "DELETE" });
      setRecords([]); setTotal(0); setCounts({}); setSelected(null); setPage(1); await refresh(); await load(); setMessage("");
    } catch (error) { setMessage(error.message); } finally { setDeletingScope(null); }
  };
  const trackCsvImport = (jobId) => {
    window.clearInterval(importTimer.current);
    const timer = window.setInterval(async () => {
      try {
        const state = await api(`/api/imports/${jobId}`); setJob(state);
        setImportProgress((current) => ({ ...current, status: state.status, completed: state.completed_rows || 0, total: state.total_rows || 0 }));
        if (state.status === "completed") {
          window.clearInterval(timer); setIsUploading(false); await refresh(); setMessage(`모든 요약이 완료되었습니다. 총 ${state.saved_rows.toLocaleString()}건의 민원이 요약 및 분류 되었습니다.`); navigate("categories", true);
        } else if (state.status === "failed") {
          window.clearInterval(timer); setIsUploading(false); setMessage(`파일 처리에 실패했습니다. ${state.last_error || "실패 행 목록을 확인해 주세요."}`);
        }
      } catch (error) { window.clearInterval(timer); setIsUploading(false); setImportProgress((current) => ({ ...current, status: "connection_error" })); setMessage(error.message); }
    }, 1000);
    importTimer.current = timer;
  };
  const retryCsvImport = async (target = job) => {
    if (!target || target.status !== "failed") return;
    try {
      setIsUploading(true);
      const restarted = await api(`/api/imports/${target.id || target.job_id}/retry`, { method: "POST" });
      setJob(restarted); setMessage(""); setImportProgress((current) => ({ ...current, status: "queued", completed: restarted.completed_rows || 0, total: restarted.total_rows || current?.total || 0 })); trackCsvImport(restarted.id || restarted.job_id);
    } catch (error) { setIsUploading(false); setMessage(error.message); }
  };
  const confirmCsvMapping = async () => {
    if (!csvMapping) return;
    const mapping = csvMapping.mapping;
    if (!mapping.content_columns?.length) return setMessage("민원 본문 열을 하나 이상 선택해 주세요.");
    try {
      setIsUploading(true);
      const started = await api(`/api/imports/${csvMapping.job_id}/mapping`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ profile_name: csvMapping.profile_name || "", title_column: mapping.title_column || "", content_columns: mapping.content_columns, response_column: mapping.response_column || "", category_column: mapping.category_column || "", save_mapping: true }) });
      setCsvMapping(null); setJob(started); setMessage(""); setImportProgress((current) => ({ ...current, status: "queued" })); trackCsvImport(started.job_id);
    } catch (error) { setIsUploading(false); setMessage(error.message); }
  };
  const upload = async () => {
    if (!file || isUploading) return setMessage(!file ? "처리할 파일을 선택해 주세요." : "파일을 처리하고 있습니다.");
    try {
      setIsUploading(true);
      setMessage(""); setJob(null); setCsvMapping(null);
      setImportProgress({ name: file.name, completed: 0, total: 0, status: "preparing" });
      const form = new FormData(); form.append("file", file);
      if (file.name.toLowerCase().endsWith(".csv")) {
        const created = await api("/api/imports", { method: "POST", body: form }); setJob(created);
        setImportProgress({ name: file.name, completed: 0, total: created.total_rows || 0, status: created.status });
        if (created.needs_mapping) {
          setIsUploading(false);
          setCsvMapping({ ...created, profile_name: "", mapping: { ...created.mapping, content_columns: created.mapping.content_columns || [] } });
          setMessage("CSV 헤더 자동 판단의 확신이 낮습니다. 아래에서 제목·본문 열을 확인해 주세요.");
        } else {
          setCsvMapping(null); trackCsvImport(created.job_id);
        }
      } else {
        const data = await api("/api/intake", { method: "POST", body: form });
        const batchSize = Math.min(data.max_batch_size || 500, 500);
        let saved = 0, duplicates = 0;
        setImportProgress({ name: file.name, completed: 0, total: data.complaints.length, status: "processing" });
        for (let start = 0; start < data.complaints.length; start += batchSize) {
          const complaints = data.complaints.slice(start, start + batchSize);
          const result = await api("/api/complaints/batch", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ complaints }) });
          saved += result.saved; duplicates += result.duplicates;
          setImportProgress({ name: file.name, completed: Math.min(start + batchSize, data.complaints.length), total: data.complaints.length, status: "processing" });
          await refresh();
        }
        setImportProgress({ name: file.name, completed: data.complaints.length, total: data.complaints.length, status: "completed" });
        setIsUploading(false); setMessage(`모든 요약이 완료되었습니다. 총 ${saved.toLocaleString()}건의 민원이 요약 및 분류 되었습니다. 중복 제외 ${duplicates.toLocaleString()}건`); await refresh(); navigate("categories", true);
      }
    } catch (error) { setIsUploading(false); setImportProgress((current) => ({ ...current, status: "failed" })); setMessage(error.message); }
  };
  const chooseForIntake = async (record) => {
    if (!record.can_manage) return setMessage("다른 부서 민원은 원문과 상태만 확인할 수 있습니다.");
    if (record.complaint_status === "취소" || actionBusy) return;
    setActionBusy(true);
    try {
      const data = await api(`/api/department/complaints/${record.id}/start`, { method: "POST" });
      setSelected(data.complaint); setResponseText(data.complaint.latest_response_state === "draft" ? data.complaint.latest_response : ""); setTransferTarget(""); navigate("intake"); await refresh();
    } catch (error) { setMessage(error.message); } finally { setActionBusy(false); }
  };
  const saveDraft = async () => {
    if (!isOpen(selected) || actionBusy) return;
    if (!responseText.trim()) return setMessage("답변 내용을 입력해 주세요.");
    setActionBusy(true);
    try {
      await api(`/api/department/complaints/${selected.id}/responses`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ content: responseText }) });
      setSelected({ ...selected, complaint_status: "진행중", latest_response: responseText, latest_response_state: "draft" });
      setMessage("답변이 임시 저장되었습니다. 민원인에게는 아직 표시되지 않습니다."); await refresh();
    } catch (error) { setMessage(error.message); } finally { setActionBusy(false); }
  };
  const deleteDraft = async () => {
    if (!isOpen(selected) || actionBusy || !window.confirm("임시 저장한 답변을 삭제할까요?")) return;
    setActionBusy(true);
    try {
      await api(`/api/department/complaints/${selected.id}/responses/draft`, { method: "DELETE" });
      setSelected({ ...selected, latest_response: "", latest_response_state: "" }); setResponseText("");
      setMessage("임시 저장 답변을 삭제했습니다."); await refresh();
    } catch (error) { setMessage(error.message); } finally { setActionBusy(false); }
  };
  const sendResponse = async () => {
    if (!isOpen(selected) || actionBusy) return;
    setActionBusy(true);
    try {
      await api(`/api/department/complaints/${selected.id}/responses/send`, { method: "POST" });
      setSelected({ ...selected, complaint_status: "완료", latest_response_state: "sent" });
      setMessage("답변을 민원인에게 전송했습니다."); await refresh();
    } catch (error) { setMessage(error.message); } finally { setActionBusy(false); }
  };
  const transfer = async (category) => {
    if (!isOpen(selected) || actionBusy || !category || category === selected.category) return;
    setActionBusy(true);
    try {
      await api(`/api/department/complaints/${selected.id}/transfer`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ category }) });
      setSelected(null); setMessage(`${category} 부서로 전달했습니다.`); await refresh(); navigate("categories", true);
    } catch (error) { setMessage(error.message); } finally { setActionBusy(false); }
  };
  const navItems = isAdmin ? [["upload", "새 민원"], ["categories", "분류 목록"], ["deleted", "삭제된 데이터"], ["intake", "민원 접수"]] : [["write", "새 민원"], ["submitted", "작성한 민원"]];
  const pageTitle = { write: "새 민원 작성", submitted: "작성한 민원", upload: "파일에서 일괄 가져오기", categories: "분류 목록", deleted: "삭제된 데이터", intake: "민원 접수" }[view];
  const adminCategories = categories;
  const canManageCategory = isAdmin && active === auth.user.department;
  return <div className="app">
    <aside><div className="brand"><b>C</b><strong>ComplaintAI</strong></div><p className="navlabel">민원 업무</p>
      {navItems.map(([id, label]) => <button key={id} className={view === id ? "active" : ""} onClick={() => navigate(id)}>{label}</button>)}
      <div className="sidebar-bottom">
        {isAdmin && importProgress && <ImportProgress progress={importProgress} />}
        <div className="account"><p className="navlabel">계정</p><small>{auth.user.name} · {isAdmin ? `${auth.user.department} 관리자` : "민원인"}</small><button onClick={signout}>로그아웃</button><em>개인정보는 요약 결과에 노출되지 않도록 제외해 처리합니다.</em></div>
      </div>
    </aside>
    <main><header><div><span>{isAdmin ? "부서 민원 관리" : "민원인 업무"}</span><h1>{pageTitle}</h1></div><label>처리 서버 주소<input value={server} onChange={(e) => { setServer(e.target.value); localStorage.setItem("complaintai.fastapi.server", e.target.value); }} /></label></header>
      {message && <p className="notice">{message}</p>}
      {view === "write" && <section className="panel compose"><h2>{editing ? "민원 수정" : followUp ? "완료 민원에 재민원 작성" : "민원 내용을 입력하세요"}</h2>{followUp && <PreviousContext context={{ title: followUp.title, content: followUp.content, response: followUp.latest_response, previous_context: followUp.previous_context }} />}<label>민원 제목<input value={title} onChange={(e) => { setTitle(e.target.value); setAnalysis(null); }} /></label><label>민원 원문<textarea value={content} onChange={(e) => { setContent(e.target.value); setAnalysis(null); }} placeholder="민원 내용을 가능한 구체적으로 작성해 주세요." /></label><div className="actions compose-actions"><button className="primary" disabled={actionBusy} onClick={summarize}>{actionBusy ? "접수 중..." : editing ? "수정 완료" : followUp ? "재민원 접수" : "작성완료"}</button>{(editing || followUp) && <button onClick={() => { setEditing(null); setFollowUp(null); setTitle(""); setContent(""); setAnalysis(null); }}>작성 취소</button>}</div></section>}
      {view === "upload" && <section className="panel compose">
        <h2>파일에서 일괄 가져오기</h2>
        <p className="muted">각 행은 독립적인 민원으로 요약·분류되며, 담당 부서의 분류 목록에 저장됩니다.</p>
        <label className="upload">파일 선택
          <input type="file" accept=".hwp,.pdf,.png,.jpg,.jpeg,.webp,.xlsx,.xls,.csv" disabled={isUploading} onChange={(e) => setFile(e.target.files?.[0])} />
          <small>{file?.name || "HWP, PDF, 이미지, XLSX, XLS, CSV"}</small>
        </label>
        <button className="primary" disabled={isUploading} onClick={upload}>{isUploading ? "처리 중..." : "파일 일괄 처리"}</button>
        {job && <p className="muted">작업 ID: {job.id || job.job_id} · 상태: {job.status} {job.status === "failed" && <button onClick={() => retryCsvImport()}>재처리</button>}</p>}
        {csvMapping && <article className="analysis csv-mapping">
          <h3>CSV 열 매핑 확인</h3><p className="muted">자동 판단 결과를 확인하세요. 선택한 구성은 같은 헤더의 CSV에 자동 적용됩니다.</p>
          <label>매핑 이름<input value={csvMapping.profile_name} placeholder="예: 기관 A 민원 CSV" onChange={(e) => setCsvMapping({ ...csvMapping, profile_name: e.target.value })} /></label>
          <label>제목 열<select value={csvMapping.mapping.title_column || ""} onChange={(e) => setCsvMapping({ ...csvMapping, mapping: { ...csvMapping.mapping, title_column: e.target.value } })}><option value="">제목 열 없음</option>{csvMapping.headers.map((header) => <option key={header}>{header}</option>)}</select></label>
          <fieldset><legend>민원 본문 열</legend>{csvMapping.headers.map((header) => <label key={header} className="checkbox-row"><input type="checkbox" checked={csvMapping.mapping.content_columns.includes(header)} onChange={(e) => setCsvMapping({ ...csvMapping, mapping: { ...csvMapping.mapping, content_columns: e.target.checked ? [...csvMapping.mapping.content_columns, header] : csvMapping.mapping.content_columns.filter((value) => value !== header) } })} />{header}</label>)}</fieldset>
          <label>답변 열<select value={csvMapping.mapping.response_column || ""} onChange={(e) => setCsvMapping({ ...csvMapping, mapping: { ...csvMapping.mapping, response_column: e.target.value } })}><option value="">선택 안 함</option>{csvMapping.headers.map((header) => <option key={header}>{header}</option>)}</select></label>
          <label>기존 분류 열<select value={csvMapping.mapping.category_column || ""} onChange={(e) => setCsvMapping({ ...csvMapping, mapping: { ...csvMapping.mapping, category_column: e.target.value } })}><option value="">선택 안 함</option>{csvMapping.headers.map((header) => <option key={header}>{header}</option>)}</select></label>
          <button className="primary" onClick={confirmCsvMapping}>매핑 저장 후 처리 시작</button>
        </article>}
      </section>}
      {view === "submitted" && <ComplaintList records={records} total={total} page={page} pageSize={pageSize} setPage={setPage} setPageSize={setPageSize} empty="작성한 민원이 없습니다." user onEdit={edit} onDelete={remove} onFollowUp={writeFollowUp} />}
      {(view === "categories" || view === "deleted") && <><section className="category"><div className="category-head"><h2>{view === "deleted" ? "삭제된 데이터" : "분류 카테고리"}</h2></div>{view === "categories" && <div className="grid">{adminCategories.map((category) => <button key={category} className={`${active === category ? "selected" : ""} ${isAdmin && category === auth.user.department ? "department-owned" : ""}`} onClick={() => { setActive(category); setPage(1); }}><b>{counts[category] || 0}</b>{category}</button>)}</div>}</section><ComplaintList records={records} total={total} page={page} pageSize={pageSize} setPage={setPage} setPageSize={setPageSize} sourceFilter={sourceFilter} setSourceFilter={setSourceFilter} deleted={view === "deleted"} canManageCategory={canManageCategory} canRunGlobalPurge={isAdmin} deletingScope={deletingScope} empty="표시할 민원이 없습니다." onSelect={chooseForIntake} onDelete={remove} onDeleteAll={removeCategory} onDepartmentPurge={deleteDepartmentAll} onRestore={restore} onHardDelete={hardDelete} /></>}
      {view === "intake" && <section className="department-workspace">
        <article className="panel intake">
          {!selected ? <><h2>민원 접수</h2><p className="muted">민원 접수는 분류 목록에서 선택한 민원만 처리할 수 있습니다.</p><button className="primary" onClick={() => navigate("categories")}>분류 목록 이동</button></> : selected.complaint_status === "취소" || selected.deleted_at || !selected.can_manage ? <article className="completion" role="alert"><h3>{selected.complaint_status === "취소" ? "해당 민원은 삭제(취소)되었습니다." : "해당 민원을 더 이상 처리할 수 없습니다."}</h3><p>{selected.cancelled_by_role === "user" ? "민원인이 해당 민원을 취소했습니다." : selected.cancellation_reason || "담당 부서가 변경되었거나 민원이 삭제되었습니다."}</p><button onClick={() => { setSelected(null); setResponseText(""); navigate("categories"); }}>분류 목록으로 이동</button></article> : <>
            <h2>{selected.title}</h2>
            <p className="muted">{selected.category} · {selected.submitted_by_user ? "일반 사용자 민원" : "파일 민원"}</p>
            <h3>원본 민원</h3><p className="original">{selected.content}</p>
            <PreviousContext context={selected.previous_context} />
            <h3>요약</h3><p>{selected.summary}</p>
            <p>민원 상태: <b>{statusLabel(selected.complaint_status)}</b></p>
            {selected.latest_response && <article className={selected.latest_response_state === "draft" ? "answer draft-answer" : "answer"}><b>{selected.latest_response_state === "draft" ? "임시 저장 답변 · 관리자만 확인 가능" : "답변 전송 완료"}</b><p>{selected.latest_response}</p></article>}
            {selected.complaint_status === "완료" ? <article className="completion"><h3>민원 답변이 완료되었습니다.</h3><p>새로운 민원 접수를 하시겠습니까?</p><button className="primary" onClick={() => { setSelected(null); setResponseText(""); navigate("categories"); }}>분류 목록으로 이동</button></article> : <><label>답변 내용<textarea disabled={actionBusy} value={responseText} onChange={(e) => setResponseText(e.target.value)} placeholder="민원인에게 전달할 답변을 작성해 주세요." /></label>{selected.latest_response_state === "draft" ? <div className="actions"><button disabled={actionBusy} className="danger-button" onClick={deleteDraft}>임시 저장 삭제</button><button disabled={actionBusy} className="primary" onClick={sendResponse}>{actionBusy ? "처리 중..." : "답변 전송"}</button></div> : <button disabled={actionBusy} className="primary" onClick={saveDraft}>{actionBusy ? "처리 중..." : "답변 임시 저장"}</button>}</>}
          </>}
        </article>
        {isOpen(selected) && !selected.deleted_at && selected.can_manage && <article className="panel transfer-panel">
          <h2>다른 부서로 전달</h2>
          <p className="muted">전달하면 민원은 선택한 부서의 분류 목록으로 이동하고 상태는 접수 대기로 변경됩니다.</p>
          <div className="transfer-controls">
            <label>전달할 부서<select value={transferTarget} onChange={(e) => setTransferTarget(e.target.value)}><option value="">부서를 선택하세요</option>{categories.filter((category) => category !== selected.category).map((category) => <option key={category}>{category}</option>)}</select></label>
            <button className="primary" disabled={!transferTarget || actionBusy} onClick={() => transfer(transferTarget)}>부서 전달</button>
          </div>
        </article>}
      </section>}
    </main>
  </div>;
}

function ImportProgress({ progress }) {
  const percentage = importPercentage(progress);
  const status = { preparing: "파일 준비", queued: "대기 중", processing: "처리 중", running: "처리 중", awaiting_mapping: "열 매핑 확인 필요", completed: "처리 완료", failed: "처리 실패", connection_error: "연결 확인 필요" }[progress.status] || "처리 중";
  return <section className={`import-progress${progress.status === "completed" ? " completed" : ""}`} aria-label="파일 처리 진행률">
    <div className="import-progress-heading"><span>{status}</span><strong>{percentage}%</strong></div>
    <div className="import-progress-track" role="progressbar" aria-label="민원 파일 처리" aria-valuemin={0} aria-valuemax={100} aria-valuenow={percentage} aria-valuetext={`${status}, ${percentage}%`}><div className="import-progress-fill" style={{ width: `${percentage}%` }} /></div>
    <small className="import-progress-file" title={progress.name}>{progress.name}</small>
    <small>{progress.total > 0 ? `${(progress.completed || 0).toLocaleString()} / ${progress.total.toLocaleString()}건` : "전체 건수 확인 중"}</small>
  </section>;
}

function PreviousContext({ context }) {
  if (!context) return null;
  return <details className="answer previous-context" open><summary>이전 민원 및 완료 답변</summary><h4>{context.title}</h4><p className="original">{context.content}</p><b>이전 답변</b><p className="original">{context.response}</p>{context.previous_context && <PreviousContext context={context.previous_context} />}</details>;
}

function ComplaintList({ records, total, page, pageSize, setPage, setPageSize, sourceFilter, setSourceFilter, empty, user, deleted, canManageCategory, canRunGlobalPurge, deletingScope, onEdit, onDelete, onDeleteAll, onDepartmentPurge, onRestore, onHardDelete, onSelect, onFollowUp }) {
  const [cancellingId, setCancellingId] = useState(null);
  const [cancelReason, setCancelReason] = useState("");
  const [cancelBusy, setCancelBusy] = useState(false);
  useEffect(() => { setCancellingId(null); setCancelReason(""); }, [page, pageSize, sourceFilter, deleted, user]);
  const toggleCancellation = (id) => {
    if (cancelBusy) return;
    setCancellingId((current) => current === id ? null : id);
    setCancelReason("해당 민원은 잘못된 사유로 인해 취소되었습니다.");
  };
  const sendCancellation = async (event, id) => {
    event.preventDefault();
    if (cancelBusy || !cancelReason.trim()) return;
    setCancelBusy(true);
    try {
      if (await onDelete(id, cancelReason)) { setCancellingId(null); setCancelReason(""); }
    } finally { setCancelBusy(false); }
  };
  return <section className="panel">
    <div className="list-head"><h2>{deleted ? "삭제된 데이터 보관함" : user ? "내 민원 목록" : "부서 민원 보관함"}</h2><label>한 번에 보기<select value={pageSize} onChange={(e) => { setPageSize(+e.target.value); setPage(1); }}>{[10, 20, 50, 100].map((value) => <option key={value}>{value}</option>)}</select></label></div>
    {!user && <div className="source-filter" role="group" aria-label="민원 출처 필터">{[["all", "전체"], ["user", "민원인 작성"], ["file", "파일 업로드"]].map(([value, label]) => <button key={value} aria-pressed={sourceFilter === value} className={sourceFilter === value ? "selected" : ""} onClick={() => { setSourceFilter(value); setPage(1); }}>{label}</button>)}<small>파일 업로드 민원은 옅은 회색으로 표시됩니다.</small></div>}
    <div className="list">{records.map((record) => {
      const cancelled = record.complaint_status === "취소";
      return <article key={record.id} className={!record.submitted_by_user ? "file-complaint" : "user-complaint"}><div className="record-row"><div>
        <h3>{record.title}</h3><small><span className="source-badge">{record.submitted_by_user ? "민원인 작성" : "파일 업로드"}</span> {record.category || "부서 판별중..."} · 상태: <b>{statusLabel(record.complaint_status)}</b> · {new Date(record.created_at).toLocaleDateString()}</small>
        {cancelled ? <div className="answer cancellation" role="status"><b>{record.cancelled_by_role === "user" ? "민원인이 해당 민원을 취소했습니다." : "민원이 취소되었습니다."}</b>{record.cancelled_by_role === "admin" && <p>취소 사유: {record.cancellation_reason || "사유가 기록되지 않았습니다."}</p>}</div> : <>
          {user ? <p className="original">{record.content}</p> : <><p>{record.summary}</p><details><summary>원본 민원 확인</summary><p className="original">{record.content}</p></details></>}
          <PreviousContext context={record.previous_context} />
          {record.latest_response && <div className={record.latest_response_state === "draft" ? "answer draft-answer" : "answer"}><b>{record.latest_response_state === "draft" ? "임시 저장 답변 · 관리자만 확인 가능" : "답변 완료"}</b><p className="original">{record.latest_response}</p></div>}
        </>}
      </div><div className="record-actions">
        {cancelled ? <span className="read-only">취소 · 처리 불가</span> : deleted ? record.can_manage ? <><button onClick={() => onRestore(record.id)}>복원</button>{record.complaint_status !== "완료" && <button className="danger" onClick={() => onHardDelete(record.id)}>영구 삭제</button>}</> : <span className="read-only">읽기 전용</span> : user ? <>
          {record.complaint_status === "접수" && <button onClick={() => onEdit(record)}>수정</button>}
          {record.complaint_status === "진행중" && <span className="read-only">관리자가 접수를 시작하여 수정할 수 없습니다.</span>}
          {isOpen(record) && <button className="danger" onClick={() => onDelete(record.id)}>삭제</button>}
          {record.complaint_status === "완료" && <button className="primary" onClick={() => onFollowUp(record)}>재민원 작성</button>}
        </> : record.can_manage ? <>
          <button className="primary" onClick={() => onSelect(record)}>{record.complaint_status === "완료" ? "완료 답변 확인" : "민원 접수"}</button>
          {isOpen(record) && <button className="danger" disabled={cancelBusy} aria-expanded={cancellingId === record.id} aria-controls={`cancel-reason-${record.id}`} onClick={() => toggleCancellation(record.id)}>삭제</button>}
        </> : <span className="read-only">읽기 전용</span>}
      </div></div>
      {!user && !deleted && record.can_manage && isOpen(record) && cancellingId === record.id && <form id={`cancel-reason-${record.id}`} className="cancel-reason-editor" onSubmit={(event) => sendCancellation(event, record.id)}>
        <label htmlFor={`cancel-reason-input-${record.id}`}>삭제 사유<textarea id={`cancel-reason-input-${record.id}`} autoFocus required maxLength={2000} disabled={cancelBusy} value={cancelReason} onChange={(event) => setCancelReason(event.target.value)} /></label>
        <p className="muted">전달하면 해당 민원이 취소되며, 작성한 민원인에게 이 사유가 표시됩니다.</p>
        <div className="actions compose-actions"><button className="primary" disabled={cancelBusy || !cancelReason.trim()} type="submit">{cancelBusy ? "전달 중..." : "전달"}</button></div>
      </form>}
      </article>;
    })}</div>
    {!records.length && <p>{empty}</p>}
    <footer>{deleted && canRunGlobalPurge && <button className="danger" disabled={Boolean(deletingScope)} onClick={() => onHardDelete("all")}>{deletingScope === "permanent" ? "영구 삭제 중..." : "삭제된 데이터 전체 영구 삭제 (테스트)"}</button>}{!deleted && !user && <>{canManageCategory && <button className="danger" disabled={total === 0 || Boolean(deletingScope)} onClick={onDeleteAll}>{deletingScope === "category" ? "삭제 중..." : "선택 카테고리 삭제"}</button>}{canRunGlobalPurge && <button className="danger" disabled={Boolean(deletingScope)} onClick={onDepartmentPurge}>{deletingScope === "all" ? "삭제 중..." : "분류 카테고리 전체 삭제 (테스트)"}</button>}</>}<span>{total}건</span><button disabled={page <= 1} onClick={() => setPage(page - 1)}>이전</button><button disabled={page * pageSize >= total} onClick={() => setPage(page + 1)}>다음</button></footer>
  </section>;
}
