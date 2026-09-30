import { useCallback, useEffect, useMemo, useState } from "react";

const categories = ["행정·안전", "국토·교통", "주택건축", "환경·위생", "보건복지", "소방", "기타"];
const statuses = ["접수", "진행중", "완료", "취소"];
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
  const [file, setFile] = useState(null);
  const [job, setJob] = useState(null);
  const [csvMapping, setCsvMapping] = useState(null);
  const [isUploading, setIsUploading] = useState(false);
  const [counts, setCounts] = useState({});
  const [active, setActive] = useState(auth.user.department || "기타");
  const [records, setRecords] = useState([]);
  const [total, setTotal] = useState(0);
  const [pageSize, setPageSize] = useState(10);
  const [page, setPage] = useState(1);
  const [selected, setSelected] = useState(null);
  const [responseText, setResponseText] = useState("");
  const [transferTarget, setTransferTarget] = useState("");
  const [deletingScope, setDeletingScope] = useState(null);

  const refresh = useCallback(async () => {
    const data = await api("/api/complaints/counts");
    setCounts(Object.fromEntries(data.categories.map((item) => [item.category, item.count])));
  }, [api]);
  const load = useCallback(async () => {
    const path = isAdmin
      ? `/api/complaints?deleted=${view === "deleted"}&category=${encodeURIComponent(view === "deleted" ? "" : active)}&limit=${pageSize}&offset=${(page - 1) * pageSize}`
      : `/api/complaints?deleted=false&limit=${pageSize}&offset=${(page - 1) * pageSize}`;
    const data = await api(path); setRecords(data.complaints); setTotal(data.total);
  }, [active, api, isAdmin, page, pageSize, view]);
  useEffect(() => { refresh().catch((error) => setMessage(error.message)); }, [refresh]);
  useEffect(() => {
    if (view === "categories" || view === "deleted" || view === "submitted") load().catch((error) => setMessage(error.message));
  }, [load, view]);
  const navigate = (next, keepMessage = false) => { setView(next); if (!keepMessage) setMessage(""); };

  const summarize = async () => {
    try {
      setMessage("요약과 분류를 진행 중입니다…");
      const data = await api("/api/analyze", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ title, content }) });
      setAnalysis(data.analysis); setMessage("요약이 완료되었습니다. 내용을 확인한 뒤 접수해 주세요.");
    } catch (error) { setMessage(error.message); }
  };
  const submitComplaint = async () => {
    if (!analysis) return;
    try {
      setMessage("민원을 접수하고 있습니다…");
      if (editing) {
        await api(`/api/complaints/${editing.id}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ title, content }) });
      } else {
        await api("/api/complaints/batch", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ complaints: [{ title: analysis.title, content: analysis.content, category: analysis.category, use_ai: false }] }) });
      }
      await refresh(); setTitle(""); setContent(""); setAnalysis(null); setEditing(null);
      setMessage(editing ? "민원이 수정되었습니다." : "민원이 접수되었습니다."); navigate("submitted", true);
    } catch (error) { setMessage(error.message); }
  };
  const edit = (record) => { setEditing(record); setTitle(record.title); setContent(record.content); setAnalysis(null); navigate("write"); };
  const remove = async (id) => {
    if (!window.confirm("이 민원을 삭제할까요?")) return;
    try { await api(`/api/complaints/${id}`, { method: "DELETE" }); await refresh(); await load(); setMessage("삭제가 반영되었습니다."); } catch (error) { setMessage(error.message); }
  };
  const removeCategory = async () => {
    if (!active || total === 0) return;
    if (!window.confirm(`${active} 부서 민원 보관함의 ${total}건을 삭제합니다. 정말로 전체 삭제하시겠습니까?`)) return;
    setDeletingScope("category");
    try {
      const result = await api(`/api/complaints/category/${encodeURIComponent(active)}`, { method: "DELETE" });
      setPage(1); await refresh(); await load();
      setMessage(`${active} 부서 민원 ${result.deleted.toLocaleString()}건을 삭제된 데이터로 옮겼습니다.`);
    } catch (error) { setMessage(error.message); } finally { setDeletingScope(null); }
  };
  const restore = async (id) => { try { await api(`/api/complaints/${id}/restore`, { method: "POST" }); await refresh(); await load(); } catch (error) { setMessage(error.message); } };
  const hardDelete = async (id) => {
    const password = window.prompt("로그인 비밀번호를 입력하세요."); if (!password) return;
    try { await api(`/api/complaints/${id}/permanent`, { method: "DELETE", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ password }) }); await refresh(); await load(); } catch (error) { setMessage(error.message); }
  };
  const deleteDepartmentAll = async () => {
    if (!isAdmin) return;
    if (!window.confirm("테스트 전용 기능입니다. 7개 분류 카테고리에 표시되는 모든 민원을 삭제된 데이터 보관함으로 옮깁니다. 분류 카테고리 건수는 모두 0건이 됩니다. 계속하시겠습니까?")) return;
    setDeletingScope("all");
    try {
      const result = await api("/api/complaints/department/all", { method: "DELETE" });
      setPage(1); await refresh(); await load(); setMessage("");
    } catch (error) { setMessage(error.message); } finally { setDeletingScope(null); }
  };
  const trackCsvImport = (jobId) => {
    const timer = window.setInterval(async () => {
      try {
        const state = await api(`/api/imports/${jobId}`); setJob(state);
        if (state.status === "completed") {
          window.clearInterval(timer); setIsUploading(false); await refresh(); setMessage(`모든 요약이 완료되었습니다. 총 ${state.saved_rows.toLocaleString()}건의 민원이 요약 및 분류 되었습니다.`); navigate("categories", true);
        } else if (state.status === "failed") {
          window.clearInterval(timer); setIsUploading(false); setMessage(`파일 처리에 실패했습니다. ${state.last_error || "실패 행 목록을 확인해 주세요."}`);
        } else {
          const progress = state.completed_rows || 0;
          setMessage(state.status === "queued"
            ? "파일 처리 작업이 대기열에 등록되었습니다."
            : `현재 ${progress.toLocaleString()} / ${state.total_rows.toLocaleString()}건을 요약·분류 중입니다. 저장 완료 ${state.saved_rows.toLocaleString()}건`);
        }
      } catch (error) { window.clearInterval(timer); setIsUploading(false); setMessage(error.message); }
    }, 1000);
  };
  const retryCsvImport = async (target = job) => {
    if (!target || target.status !== "failed") return;
    try {
      setIsUploading(true);
      const restarted = await api(`/api/imports/${target.id || target.job_id}/retry`, { method: "POST" });
      setJob(restarted); setMessage("마지막 저장 지점부터 CSV 작업을 다시 대기열에 등록했습니다."); trackCsvImport(restarted.id || restarted.job_id);
    } catch (error) { setIsUploading(false); setMessage(error.message); }
  };
  const confirmCsvMapping = async () => {
    if (!csvMapping) return;
    const mapping = csvMapping.mapping;
    if (!mapping.content_columns?.length) return setMessage("민원 본문 열을 하나 이상 선택해 주세요.");
    try {
      setIsUploading(true);
      const started = await api(`/api/imports/${csvMapping.job_id}/mapping`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ profile_name: csvMapping.profile_name || "", title_column: mapping.title_column || "", content_columns: mapping.content_columns, response_column: mapping.response_column || "", category_column: mapping.category_column || "", save_mapping: true }) });
      setCsvMapping(null); setJob(started); setMessage("헤더 매핑을 저장했습니다. 행별 요약·분류를 시작합니다."); trackCsvImport(started.job_id);
    } catch (error) { setIsUploading(false); setMessage(error.message); }
  };
  const upload = async () => {
    if (!file || isUploading) return setMessage(!file ? "처리할 파일을 선택해 주세요." : "파일을 처리하고 있습니다.");
    try {
      setIsUploading(true);
      setMessage("현재 0건의 민원이 요약 및 분류 되었습니다. 파일을 준비하고 있습니다.");
      const form = new FormData(); form.append("file", file);
      if (file.name.toLowerCase().endsWith(".csv")) {
        const created = await api("/api/imports", { method: "POST", body: form }); setJob(created);
        if (created.needs_mapping) {
          setIsUploading(false);
          setCsvMapping({ ...created, profile_name: "", mapping: { ...created.mapping, content_columns: created.mapping.content_columns || [] } });
          setMessage("CSV 헤더 자동 판단의 확신이 낮습니다. 아래에서 제목·본문 열을 확인해 주세요.");
        } else {
          setCsvMapping(null); setMessage("CSV 헤더를 확인했습니다. 행별 요약·분류를 시작합니다."); trackCsvImport(created.job_id);
        }
      } else {
        const data = await api("/api/intake", { method: "POST", body: form });
        const result = await api("/api/complaints/batch", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ complaints: data.complaints }) });
        setIsUploading(false); setMessage(`모든 요약이 완료되었습니다. 총 ${result.saved.toLocaleString()}건의 민원이 요약 및 분류 되었습니다.`); await refresh(); navigate("categories", true);
      }
    } catch (error) { setIsUploading(false); setMessage(error.message); }
  };
  const chooseForIntake = (record) => { if (!record.can_manage) return setMessage("다른 부서 민원은 원문과 상태만 확인할 수 있습니다."); setSelected(record); setResponseText(record.latest_response_state === "draft" ? record.latest_response : ""); setTransferTarget(""); navigate("intake"); };
  const changeStatus = async (value) => {
    if (!selected) return;
    try { const data = await api(`/api/department/complaints/${selected.id}/status`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ status: value }) }); setSelected({ ...selected, ...data.complaint }); await refresh(); } catch (error) { setMessage(error.message); }
  };
  const saveDraft = async () => {
    if (!selected || !responseText.trim()) return setMessage("답변 내용을 입력해 주세요.");
    try {
      await api(`/api/department/complaints/${selected.id}/responses`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ content: responseText }) });
      setSelected({ ...selected, complaint_status: "진행중", latest_response: responseText, latest_response_state: "draft" });
      setMessage("답변이 임시 저장되었습니다. 민원인에게는 아직 표시되지 않습니다."); await refresh();
    } catch (error) { setMessage(error.message); }
  };
  const deleteDraft = async () => {
    if (!selected || !window.confirm("임시 저장한 답변을 삭제할까요?")) return;
    try {
      await api(`/api/department/complaints/${selected.id}/responses/draft`, { method: "DELETE" });
      setSelected({ ...selected, complaint_status: "접수", latest_response: "", latest_response_state: "" }); setResponseText("");
      setMessage("임시 저장 답변을 삭제했습니다."); await refresh();
    } catch (error) { setMessage(error.message); }
  };
  const sendResponse = async () => {
    if (!selected) return;
    try {
      await api(`/api/department/complaints/${selected.id}/responses/send`, { method: "POST" });
      setSelected({ ...selected, complaint_status: "완료", latest_response_state: "sent" });
      setMessage("답변을 민원인에게 전송했습니다."); await refresh();
    } catch (error) { setMessage(error.message); }
  };
  const transfer = async (category) => {
    if (!selected || !category || category === selected.category) return;
    try {
      await api(`/api/department/complaints/${selected.id}/transfer`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ category }) });
      setSelected(null); setMessage(`${category} 부서로 전달했습니다.`); await refresh(); navigate("categories", true);
    } catch (error) { setMessage(error.message); }
  };
  const navItems = isAdmin ? [["upload", "새 민원"], ["categories", "분류 목록"], ["deleted", "삭제된 데이터"], ["intake", "민원 접수"]] : [["write", "새 민원"], ["submitted", "작성한 민원"]];
  const pageTitle = { write: "새 민원 작성", submitted: "작성한 민원", upload: "파일에서 일괄 가져오기", categories: "분류 목록", deleted: "삭제된 데이터", intake: "민원 접수" }[view];
  const adminCategories = categories;
  const canManageCategory = isAdmin && active === auth.user.department;
  return <div className="app">
    <aside><div className="brand"><b>C</b><strong>ComplaintAI</strong></div><p className="navlabel">민원 업무</p>
      {navItems.map(([id, label]) => <button key={id} className={view === id ? "active" : ""} onClick={() => navigate(id)}>{label}</button>)}
      <div className="account"><p className="navlabel">계정</p><small>{auth.user.name} · {isAdmin ? `${auth.user.department} 관리자` : "민원인"}</small><button onClick={signout}>로그아웃</button><em>개인정보는 요약 결과에 노출되지 않도록 제외해 처리합니다.</em></div>
    </aside>
    <main><header><div><span>{isAdmin ? "부서 민원 관리" : "민원인 업무"}</span><h1>{pageTitle}</h1></div><label>처리 서버 주소<input value={server} onChange={(e) => { setServer(e.target.value); localStorage.setItem("complaintai.fastapi.server", e.target.value); }} /></label></header>
      {message && <p className="notice">{message}</p>}
      {view === "write" && <section className="panel compose"><h2>{editing ? "민원 수정" : "민원 내용을 입력하세요"}</h2><label>민원 제목<input value={title} onChange={(e) => setTitle(e.target.value)} /></label><label>민원 원문<textarea value={content} onChange={(e) => setContent(e.target.value)} placeholder="민원 내용을 가능한 구체적으로 작성해 주세요." /></label><div className="actions"><button className="primary" onClick={summarize}>요약하기</button>{editing && <button onClick={() => { setEditing(null); setTitle(""); setContent(""); setAnalysis(null); }}>수정 취소</button>}</div>{analysis && <article className="analysis"><h3>{analysis.title}</h3><p>{analysis.summary}</p><div className="chips"><span>{analysis.category}</span><span>{analysis.urgency}</span></div><button className="primary" onClick={submitComplaint}>{editing ? "수정 내용 저장" : "민원 접수"}</button></article>}</section>}
      {view === "upload" && <section className="panel compose">
        <h2>파일에서 일괄 가져오기</h2>
        <p className="muted">각 행은 독립적인 민원으로 요약·분류되며, 담당 부서의 분류 목록에 저장됩니다.</p>
        <label className="upload">파일 선택
          <input type="file" accept=".hwp,.pdf,.png,.jpg,.jpeg,.webp,.xlsx,.xls,.csv" disabled={isUploading} onChange={(e) => setFile(e.target.files?.[0])} />
          <small>{file?.name || "HWP, PDF, 이미지, XLSX, XLS, CSV"}</small>
        </label>
        <button className="primary" disabled={isUploading} onClick={upload}>{isUploading ? "처리 중..." : "파일 일괄 처리"}</button>
        {job && <p className="muted">작업 ID: {job.id || job.job_id} · 상태: {job.status} {job.status === "failed" && <button onClick={retryCsvImport}>재처리</button>}</p>}
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
      {view === "submitted" && <ComplaintList records={records} total={total} page={page} pageSize={pageSize} setPage={setPage} setPageSize={setPageSize} empty="작성한 민원이 없습니다." user onEdit={edit} onDelete={remove} />}
      {(view === "categories" || view === "deleted") && <><section className="category"><div className="category-head"><h2>{view === "deleted" ? "삭제된 데이터" : "분류 카테고리"}</h2></div>{view === "categories" && <div className="grid">{adminCategories.map((category) => <button key={category} className={`${active === category ? "selected" : ""} ${isAdmin && category === auth.user.department ? "department-owned" : ""}`} onClick={() => { setActive(category); setPage(1); }}><b>{counts[category] || 0}</b>{category}</button>)}</div>}</section><ComplaintList records={records} total={total} page={page} pageSize={pageSize} setPage={setPage} setPageSize={setPageSize} deleted={view === "deleted"} canManageCategory={canManageCategory} canRunGlobalPurge={isAdmin} deletingScope={deletingScope} empty="표시할 민원이 없습니다." onSelect={chooseForIntake} onDelete={remove} onDeleteAll={removeCategory} onDepartmentPurge={deleteDepartmentAll} onRestore={restore} onHardDelete={hardDelete} /></>}
      {view === "intake" && <section className="department-workspace">
        <article className="panel intake">
          {!selected ? <><h2>민원 접수</h2><p className="muted">민원 접수는 분류 목록에서 선택한 민원만 처리할 수 있습니다.</p><button className="primary" onClick={() => navigate("categories")}>분류 목록 이동</button></> : <>
            <h2>{selected.title}</h2>
            <p className="muted">{selected.category} · {selected.submitted_by_user ? "일반 사용자 민원" : "파일 민원"}</p>
            <h3>원본 민원</h3><p className="original">{selected.content}</p>
            <h3>요약</h3><p>{selected.summary}</p>
            <label>민원 상태<select value={selected.complaint_status} onChange={(e) => changeStatus(e.target.value)}>{statuses.map((value) => <option key={value}>{value}</option>)}</select></label>
            {selected.latest_response && <article className={selected.latest_response_state === "draft" ? "answer draft-answer" : "answer"}><b>{selected.latest_response_state === "draft" ? "임시 저장 답변 · 관리자만 확인 가능" : "답변 전송 완료"}</b><p>{selected.latest_response}</p></article>}
            {selected.latest_response_state === "sent" ? <article className="completion"><h3>민원 답변이 완료되었습니다.</h3><p>새로운 민원 접수를 하시겠습니까?</p><button className="primary" onClick={() => { setSelected(null); setResponseText(""); navigate("categories"); }}>분류 목록으로 이동</button></article> : <><label>답변 내용<textarea value={responseText} onChange={(e) => setResponseText(e.target.value)} placeholder="민원인에게 전달할 답변을 작성해 주세요." /></label>{selected.latest_response_state === "draft" ? <div className="actions"><button className="danger-button" onClick={deleteDraft}>임시 저장 삭제</button><button className="primary" onClick={sendResponse}>답변 전송</button></div> : <button className="primary" onClick={saveDraft}>답변 완료</button>}</>}
          </>}
        </article>
        {selected && selected.latest_response_state !== "sent" && <article className="panel transfer-panel">
          <h2>다른 부서로 전달</h2>
          <p className="muted">전달하면 민원은 선택한 부서의 분류 목록으로 이동하고 상태는 접수로 변경됩니다.</p>
          <div className="transfer-controls">
            <label>전달할 부서<select value={transferTarget} onChange={(e) => setTransferTarget(e.target.value)}><option value="">부서를 선택하세요</option>{categories.filter((category) => category !== selected.category).map((category) => <option key={category}>{category}</option>)}</select></label>
            <button className="primary" disabled={!transferTarget} onClick={() => transfer(transferTarget)}>부서 전달</button>
          </div>
        </article>}
      </section>}
    </main>
  </div>;
}

function ComplaintList({ records, total, page, pageSize, setPage, setPageSize, empty, user, deleted, canManageCategory, canRunGlobalPurge, deletingScope, onEdit, onDelete, onDeleteAll, onDepartmentPurge, onRestore, onHardDelete, onSelect }) {
  return <section className="panel"><div className="list-head"><h2>{deleted ? "삭제된 데이터 보관함" : user ? "내 민원 목록" : "부서 민원 보관함"}</h2><label>한 번에 보기<select value={pageSize} onChange={(e) => { setPageSize(+e.target.value); setPage(1); }}>{[10, 20, 50, 100].map((value) => <option key={value}>{value}</option>)}</select></label></div><div className="list">{records.map((record) => <article key={record.id}><div><h3>{record.title}</h3><p>{record.summary}</p><small>{record.category} · 상태: <b>{record.complaint_status}</b> · {new Date(record.created_at).toLocaleDateString()}</small><details><summary>원본 민원 확인</summary><p className="original">{record.content}</p></details>{record.latest_response && <div className={record.latest_response_state === "draft" ? "answer draft-answer" : "answer"}><b>{record.latest_response_state === "draft" ? "임시 저장 답변 · 관리자만 확인 가능" : "답변 완료"}</b><p>{record.latest_response}</p></div>}</div><div className="record-actions">{deleted ? record.can_manage ? <><button onClick={() => onRestore(record.id)}>복원</button><button className="danger" onClick={() => onHardDelete(record.id)}>영구 삭제</button></> : <span className="read-only">읽기 전용</span> : user ? <><button onClick={() => onEdit(record)}>수정</button><button className="danger" onClick={() => onDelete(record.id)}>삭제</button></> : record.can_manage ? <><button className="primary" onClick={() => onSelect(record)}>민원 접수</button><button className="danger" onClick={() => onDelete(record.id)}>삭제</button></> : <span className="read-only">읽기 전용</span>}</div></article>)}</div>{!records.length && <p>{empty}</p>}<footer>{!deleted && !user && <>{canManageCategory && <button className="danger" disabled={total === 0 || Boolean(deletingScope)} onClick={onDeleteAll}>{deletingScope === "category" ? "삭제 중..." : "선택 카테고리 삭제"}</button>}{canRunGlobalPurge && <button className="danger" disabled={Boolean(deletingScope)} onClick={onDepartmentPurge}>{deletingScope === "all" ? "삭제 중..." : "분류 카테고리 전체 삭제 (테스트)"}</button>}</>}<span>{total}건</span><button disabled={page <= 1} onClick={() => setPage(page - 1)}>이전</button><button disabled={page * pageSize >= total} onClick={() => setPage(page + 1)}>다음</button></footer></section>;
}
