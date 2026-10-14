import { useCallback, useEffect, useRef, useState } from "react";
import ComplaintFilters, { appendComplaintFilters, citizenViewStatusLabel, normalizeComplaintFilters } from "./ComplaintFilters.jsx";
import "./citizenTools.css";

const pageSize = 10;
const available = (record) => record && !record.deleted_at && record.complaint_status !== "취소";
const editable = (record) => available(record) && record.complaint_status === "접수";
const removable = (record) => available(record) && ["접수", "진행중"].includes(record.complaint_status);
const jsonRequest = (method, body) => ({ method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

// Closing this region only hides it: the parent should keep it mounted so that
// a citizen's unsaved draft and selected complaint survive the tool toggle.
export default function CitizenComplaintTools({ api, request, onContextChange, onChanged, onRecommend, onClose, visible = true, recommendationBusy = false }) {
  const [mode, setMode] = useState("list");
  const [selected, setSelected] = useState(null);
  const [title, setTitle] = useState("");
  const [content, setContent] = useState("");
  const [records, setRecords] = useState([]);
  const [page, setPage] = useState(1);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [deleteConfirm, setDeleteConfirm] = useState(false);
  const [accessBlocked, setAccessBlocked] = useState(false);
  const [listKind, setListKind] = useState("normal");
  const [appliedFilters, setAppliedFilters] = useState(null);
  const [filterInitial, setFilterInitial] = useState(null);
  const [filtersOpen, setFiltersOpen] = useState(false);
  const [search, setSearch] = useState({ state: null, query: "", complaints: [], similar_complaints: [] });
  const [similarPage, setSimilarPage] = useState(1);
  const [processedRequest, setProcessedRequest] = useState(null);
  const mutationLock = useRef(false);
  const listSequence = useRef(0);
  const detailSequence = useRef(0);
  const searchReadSequence = useRef(0);
  const searchReading = useRef(false);
  const handledRequest = useRef(null);
  const selectedRef = useRef(null);
  const modeRef = useRef(mode);
  const listKindRef = useRef(listKind);
  const searchRef = useRef(search);
  const searchPages = useRef({ page, similarPage });
  const mounted = useRef(true);
  const callbacks = useRef({ onContextChange, onChanged, onRecommend });
  callbacks.current = { onContextChange, onChanged, onRecommend };
  modeRef.current = mode;
  listKindRef.current = listKind;
  searchRef.current = search;
  searchPages.current = { page, similarPage };
  const requestSignature = request ? `${request.key}:${request.intent}:${request.intent === "search" ? request.state || "" : ""}` : null;
  const incomingRequest = Boolean(request && processedRequest !== requestSignature);

  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; handledRequest.current = null; listSequence.current += 1; detailSequence.current += 1; searchReadSequence.current += 1; };
  }, []);

  const commitSelected = useCallback((record) => { selectedRef.current = record; setSelected(record); setAccessBlocked(false); }, []);
  const commitSearch = useCallback((value) => {
    const next = typeof value === "function" ? value(searchRef.current) : value;
    searchRef.current = next; setSearch(next);
  }, []);
  const loadList = useCallback(async (targetPage, quiet = false) => {
    const sequence = ++listSequence.current;
    if (!quiet) setLoading(true);
    try {
      const params = appendComplaintFilters(new URLSearchParams({ deleted: "false", limit: String(pageSize), offset: String((targetPage - 1) * pageSize) }), appliedFilters);
      const data = await api(`/api/complaints?${params}`);
      if (!mounted.current || sequence !== listSequence.current) return;
      setRecords(data.complaints || []); setTotal(data.total || 0);
      if (data.total > 0 && (targetPage - 1) * pageSize >= data.total) setPage(Math.max(1, Math.ceil(data.total / pageSize)));
    } catch (failure) {
      if (mounted.current && sequence === listSequence.current) setError(failure.message);
    } finally { if (mounted.current && sequence === listSequence.current) setLoading(false); }
  }, [api, appliedFilters]);

  const refreshSearch = useCallback(async () => {
    if (searchReading.current || listKindRef.current !== "search" || searchRef.current.state !== "complete") return;
    const sequence = ++searchReadSequence.current;
    const snapshot = searchRef.current;
    const currentPages = searchPages.current;
    const visibleRows = [...snapshot.complaints.slice((currentPages.page - 1) * pageSize, currentPages.page * pageSize), ...snapshot.similar_complaints.slice((currentPages.similarPage - 1) * pageSize, currentPages.similarPage * pageSize)];
    const ids = [...new Set(visibleRows.map((record) => record.id))];
    if (!ids.length) return;
    searchReading.current = true;
    try {
      const results = await Promise.allSettled(ids.map((id) => api(`/api/complaints/${id}`)));
      if (!mounted.current || sequence !== searchReadSequence.current || listKindRef.current !== "search") return;
      const current = new Map(ids.map((id, index) => [id, results[index]]));
      let readError = "";
      let textChanged = false;
      const reconcile = (rows) => rows.flatMap((record) => {
        const result = current.get(record.id);
        if (!result) return [record];
        if (result.status === "fulfilled") {
          const latest = result.value.complaint;
          if (!available(latest)) return [];
          if (record.title !== latest.title || record.content !== latest.content) { textChanged = true; return []; }
          return [{ ...record, ...latest, access_unverified: false }];
        }
        if (result.reason?.status === 404 || /민원을 찾|본인 민원|권한|삭제\(취소\)/.test(result.reason?.message || "")) return [];
        readError = result.reason?.message || "민원 상태를 확인하지 못했습니다.";
        return [{ ...record, access_unverified: true }];
      });
      const complaints = reconcile(snapshot.complaints);
      const similar = reconcile(snapshot.similar_complaints);
      commitSearch((value) => ({ ...value, complaints, similar_complaints: similar }));
      if (readError) setError(readError);
      if (textChanged) setNotice("내용이 변경된 민원을 이전 검색 결과에서 제외했습니다. 다시 검색해 주세요.");
    } finally { searchReading.current = false; }
  }, [api, commitSearch]);

  const applyIntent = useCallback((record, intent) => {
    setDeleteConfirm(false);
    if (intent === "edit") {
      if (editable(record)) { setTitle(record.title || ""); setContent(record.content || ""); setMode("edit"); }
      else { setMode("detail"); setNotice("접수 대기 상태의 민원만 수정할 수 있습니다."); }
    } else {
      setMode("detail");
      if (intent === "delete") {
        if (removable(record)) setDeleteConfirm(true);
        else setNotice("접수 대기 또는 접수 됨 상태의 민원만 삭제할 수 있습니다.");
      }
    }
  }, []);

  const selectRecord = useCallback(async (id, intent = "list") => {
    if (mutationLock.current) return;
    const sequence = ++detailSequence.current;
    setError(""); setNotice(""); setLoading(true); setDeleteConfirm(false);
    // Do not keep a previous complaint visible under a newly requested ID if
    // the new request fails its ownership or existence check.
    commitSelected(null); setMode("list");
    try {
      const data = await api(`/api/complaints/${id}`);
      if (!mounted.current || sequence !== detailSequence.current) return;
      commitSelected(data.complaint); applyIntent(data.complaint, intent);
    } catch (failure) { if (mounted.current && sequence === detailSequence.current) setError(failure.message); }
    finally { if (mounted.current && sequence === detailSequence.current) setLoading(false); }
  }, [api, applyIntent, commitSelected]);

  const newComplaint = useCallback((draft) => {
    if (mutationLock.current) return;
    detailSequence.current += 1;
    commitSelected(null); setDeleteConfirm(false); setMode("create");
    setTitle(draft?.title || ""); setContent(draft?.content || ""); setError(""); setNotice(""); setLoading(false);
  }, [commitSelected]);

  const showList = useCallback(() => {
    if (mutationLock.current) return;
    detailSequence.current += 1;
    commitSelected(null); setDeleteConfirm(false); setMode("list"); setError(""); setNotice(""); setLoading(false);
  }, [commitSelected]);

  const applyFilters = useCallback((filters) => {
    const normalized = normalizeComplaintFilters(filters);
    if (mutationLock.current || !normalized.departments.length || !normalized.view_status) return;
    searchReadSequence.current += 1;
    setListKind("normal"); setAppliedFilters(normalized); setFilterInitial(normalized); setFiltersOpen(false);
    setRecords([]); setTotal(0); setPage(1); showList();
  }, [showList]);

  const clearFilters = useCallback(() => {
    if (mutationLock.current) return;
    searchReadSequence.current += 1;
    setListKind("normal"); setAppliedFilters(null); setFilterInitial(null); setPage(1); setRecords([]); setTotal(0); showList();
  }, [showList]);

  useEffect(() => {
    if (!request || busy || handledRequest.current === requestSignature) return;
    handledRequest.current = requestSignature;
    setProcessedRequest(requestSignature);
    if (request.intent === "search") {
      searchReadSequence.current += 1;
      const data = request.results || {};
      setListKind("search"); setPage(1); setSimilarPage(1); setFiltersOpen(false); showList();
      commitSearch({ state: request.state || "loading", query: request.query || data.query || "", complaints: data.complaints || [], similar_complaints: data.similar_complaints || [] });
      if (request.state === "failed") setError(request.error || "민원을 검색하지 못했습니다. 다시 검색해 주세요.");
    } else if (request.intent === "filter") {
      const normalized = normalizeComplaintFilters(request.filters);
      if (normalized.departments.length && normalized.view_status) applyFilters(normalized);
      else { clearFilters(); setFilterInitial(normalized); setFiltersOpen(true); }
    } else if (request.intent === "create") newComplaint(request.draft);
    else if (request.complaint_id) selectRecord(request.complaint_id, request.intent);
    else {
      setListKind("normal"); showList(); setPage(1);
      if (["edit", "delete"].includes(request.intent)) setNotice(request.intent === "edit" ? "수정할 민원을 선택한 다음 ‘민원 수정’을 눌러 주세요." : "삭제할 민원을 선택한 다음 ‘민원 삭제’를 눌러 주세요.");
    }
  }, [request, requestSignature, busy, newComplaint, selectRecord, showList, applyFilters, clearFilters, commitSearch]);

  useEffect(() => {
    if (!visible || mode !== "list" || listKind !== "normal" || incomingRequest) return;
    loadList(page);
    const timer = window.setInterval(() => { if (!mutationLock.current) loadList(page, true); }, 3000);
    return () => { window.clearInterval(timer); listSequence.current += 1; };
  }, [visible, mode, page, loadList, listKind, incomingRequest]);

  useEffect(() => {
    if (!visible || mode !== "list" || listKind !== "search" || search.state !== "complete") return;
    refreshSearch().catch((failure) => { if (mounted.current) setError(failure.message); });
    const timer = window.setInterval(() => { if (!mutationLock.current) refreshSearch().catch((failure) => { if (mounted.current) setError(failure.message); }); }, 3000);
    return () => window.clearInterval(timer);
  }, [visible, mode, listKind, search.state, search.query, page, similarPage, refreshSearch]);

  useEffect(() => {
    if (listKind !== "search") return;
    setPage((current) => Math.min(current, Math.max(1, Math.ceil(search.complaints.length / pageSize))));
    setSimilarPage((current) => Math.min(current, Math.max(1, Math.ceil(search.similar_complaints.length / pageSize))));
  }, [listKind, search.complaints.length, search.similar_complaints.length]);

  const refreshSelection = useCallback(async () => {
    const current = selectedRef.current;
    if (!current) return;
    const sequence = detailSequence.current;
    let data;
    try { data = await api(`/api/complaints/${current.id}`); }
    catch (failure) {
      // Even an earlier readable snapshot must not remain actionable after a
      // failed current access/status check. A later successful poll restores it.
      if (mounted.current && sequence === detailSequence.current && selectedRef.current?.id === current.id) { setAccessBlocked(true); setDeleteConfirm(false); }
      throw failure;
    }
    if (!mounted.current || sequence !== detailSequence.current || selectedRef.current?.id !== current.id) return;
    commitSelected(data.complaint);
    if (modeRef.current === "edit" && !editable(data.complaint)) setNotice("민원 상태가 변경되어 수정할 수 없습니다. 작성 중인 내용은 보관되어 있습니다.");
    if (!removable(data.complaint)) setDeleteConfirm(false);
  }, [api, commitSelected]);

  useEffect(() => {
    if (!visible || !selected?.id) return;
    let pending = false;
    const timer = window.setInterval(async () => {
      if (pending || mutationLock.current) return;
      pending = true;
      try { await refreshSelection(); }
      catch (failure) { if (mounted.current) setError(failure.message); }
      finally { pending = false; }
    }, 3000);
    return () => window.clearInterval(timer);
  }, [visible, selected?.id, refreshSelection]);

  const inForm = ["create", "edit", "follow-up"].includes(mode);
  useEffect(() => {
    callbacks.current.onContextChange?.({
      mode: deleteConfirm ? "delete" : mode === "detail" ? "list" : mode === "follow-up" ? "create" : mode,
      complaint_id: accessBlocked ? null : selected?.id || null,
      title: accessBlocked ? "" : inForm ? title : selected?.title || "",
      content: accessBlocked ? "" : inForm ? content : available(selected) ? selected?.content || "" : "",
    });
  }, [mode, selected?.id, selected?.title, selected?.content, selected?.complaint_status, selected?.deleted_at, title, content, inForm, deleteConfirm, accessBlocked]);

  const submit = async (event) => {
    event.preventDefault();
    if (mutationLock.current || accessBlocked || !content.trim() || (mode === "edit" && !editable(selected)) || (mode === "follow-up" && (!available(selected) || selected.complaint_status !== "완료"))) return;
    const currentMode = mode;
    const parentId = selected?.id;
    const originalRecord = selected;
    const draft = { title, content };
    mutationLock.current = true; setBusy(true); setError(""); setNotice("");
    try {
      const path = currentMode === "edit" ? `/api/complaints/${parentId}` : currentMode === "follow-up" ? `/api/complaints/${parentId}/follow-up` : "/api/complaints";
      const result = await api(path, jsonRequest(currentMode === "edit" ? "PATCH" : "POST", draft));
      if (!mounted.current) return;
      const record = { ...draft, complaint_status: "접수", ...result.complaint };
      commitSelected(record); setMode("detail"); setDeleteConfirm(false); setPage(1);
      const searchChanged = listKindRef.current === "search" && currentMode === "edit" && (originalRecord?.title !== draft.title || originalRecord?.content !== draft.content);
      if (searchChanged) {
        searchReadSequence.current += 1;
        commitSearch((current) => ({ ...current, complaints: current.complaints.filter((item) => item.id !== record.id), similar_complaints: current.similar_complaints.filter((item) => item.id !== record.id) }));
      }
      const message = (result.message || (currentMode === "edit" ? "민원이 수정되었습니다." : currentMode === "follow-up" ? "이전 민원과 답변을 포함한 재민원이 접수되었습니다." : "민원이 접수되었습니다.")) + (searchChanged ? " 수정한 민원은 이전 검색 결과에서 제외했습니다. 다시 검색해 주세요." : "");
      setNotice(message);
      callbacks.current.onChanged?.({ action: currentMode === "edit" ? "edit" : currentMode === "follow-up" ? "follow-up" : "create", record, message });
      // A successful POST must remain successful even if the later detail
      // refresh fails. Never retry a completed mutation because of that read.
      if (record.id) refreshSelection().catch((failure) => { if (mounted.current) setError(`민원은 저장되었지만 최신 상태를 불러오지 못했습니다. ${failure.message}`); });
      if (listKindRef.current === "search") refreshSearch().catch((failure) => { if (mounted.current) setError(failure.message); });
    } catch (failure) {
      if (mounted.current) {
        setError(failure.message);
        if (currentMode !== "create") await refreshSelection().catch(() => {});
      }
    } finally { mutationLock.current = false; if (mounted.current) setBusy(false); }
  };

  const confirmDelete = async () => {
    if (mutationLock.current || accessBlocked || !deleteConfirm || !removable(selected)) return;
    const record = selected;
    mutationLock.current = true; setBusy(true); setError(""); setNotice("");
    try {
      const result = await api(`/api/complaints/${record.id}`, jsonRequest("DELETE", { reason: "" }));
      if (!mounted.current) return;
      detailSequence.current += 1; commitSelected(null); setDeleteConfirm(false); setMode("list"); setPage(1);
      const message = result.message || "민원이 취소되었습니다.";
      setNotice(message); callbacks.current.onChanged?.({ action: "delete", record, message });
      if (listKindRef.current === "search") {
        searchReadSequence.current += 1;
        commitSearch((current) => ({ ...current, complaints: current.complaints.filter((item) => item.id !== record.id), similar_complaints: current.similar_complaints.filter((item) => item.id !== record.id) }));
        refreshSearch().catch((failure) => { if (mounted.current) setError(failure.message); });
      } else loadList(1, true);
    } catch (failure) {
      if (mounted.current) { setError(failure.message); await refreshSelection().catch(() => {}); }
    } finally { mutationLock.current = false; if (mounted.current) setBusy(false); }
  };

  const recommendationRecord = () => inForm ? { title, content } : selected;
  const startFollowUp = () => {
    if (mutationLock.current || accessBlocked || !available(selected) || selected.complaint_status !== "완료") return;
    setTitle(`재민원: ${selected.title}`); setContent(""); setMode("follow-up"); setDeleteConfirm(false); setError(""); setNotice("");
  };

  const renderRows = (rows) => rows.map((record) => <article key={record.id} className="citizen-tools-list-item"><button type="button" disabled={busy || loading || record.access_unverified} onClick={() => selectRecord(record.id)} aria-label={`민원 #${record.id} ${record.title} 확인`}><strong>{record.title}</strong><small>#{record.id} · {record.category || "부서 판별 중"} · {citizenViewStatusLabel(record)}</small></button>{record.complaint_status === "취소" && <small className="citizen-tools-muted">취소된 민원 · 처리 불가</small>}{record.access_unverified && <small className="citizen-tools-muted">최신 상태 확인이 필요합니다.</small>}</article>);
  const renderPagination = (count, currentPage, changePage, label = "") => <footer className="citizen-tools-pagination"><span>{count}건 · {currentPage}페이지</span><button type="button" aria-label={label ? `${label} 이전` : undefined} disabled={busy || loading || currentPage <= 1} onClick={() => changePage((current) => current - 1)}>이전</button><button type="button" aria-label={label ? `${label} 다음` : undefined} disabled={busy || loading || currentPage * pageSize >= count} onClick={() => changePage((current) => current + 1)}>다음</button></footer>;

  const searchLoading = listKind === "search" && search.state === "loading";
  return <section className="citizen-tools" aria-label="민원 도구" hidden={!visible} aria-busy={busy || loading || searchLoading}>
    <header className="citizen-tools-header"><div><span>챗봇과 함께 이용하기</span><h2>민원 도구</h2></div><button type="button" className="citizen-tools-close" aria-label="도구 창 닫기" onClick={onClose}>×</button></header>
    <nav className="citizen-tools-tabs" aria-label="민원 도구 선택"><button type="button" aria-pressed={mode === "create"} disabled={busy || searchLoading} onClick={() => newComplaint()}>새 민원 작성</button><button type="button" aria-pressed={mode !== "create"} disabled={busy || searchLoading} onClick={() => { setListKind("normal"); showList(); }}>작성한 민원 확인</button></nav>
    <div className="citizen-tools-body">
      {error && <p className="citizen-tools-error" role="alert">{error}</p>}
      {notice && <p className="citizen-tools-notice" role="status">{notice}</p>}
      {accessBlocked && <p className="citizen-tools-muted" role="status">최신 민원 상태와 접근 권한을 확인하지 못했습니다. 다시 확인될 때까지 저장·삭제·AI 답변 기능을 사용할 수 없습니다.</p>}
      {loading && <p className="citizen-tools-muted" role="status">민원을 불러오는 중입니다.</p>}
      {inForm && <form onSubmit={submit} className="citizen-tools-form">
        <h3>{mode === "edit" ? `민원 #${selected?.id} 수정` : mode === "follow-up" ? "완료 민원에 재민원 작성" : "새 민원 작성"}</h3>
        <p className="citizen-tools-muted">오른쪽에서 작성하면서 챗봇에 문구나 필요한 정보를 물어보세요. 접수 버튼을 눌러야 실제 민원이 저장됩니다.</p>
        {mode === "follow-up" && <PreviousContext context={{ title: selected?.title, content: selected?.content, response: selected?.latest_response, previous_context: selected?.previous_context }} />}
        <label htmlFor="citizen-tools-title">도구 민원 제목<input id="citizen-tools-title" value={title} disabled={busy} onChange={(event) => setTitle(event.target.value)} /></label>
        <label htmlFor="citizen-tools-content">도구 민원 내용<textarea id="citizen-tools-content" value={content} disabled={busy} required onChange={(event) => setContent(event.target.value)} placeholder="발생 장소, 시각, 상황과 요청 사항을 적어 주세요." /></label>
        {mode === "edit" && !editable(selected) && <p className="citizen-tools-error">접수 대기 상태의 민원만 수정할 수 있습니다.</p>}
        {mode === "follow-up" && (!available(selected) || selected.complaint_status !== "완료") && <p className="citizen-tools-error">답변이 완료된 민원에만 재민원을 작성할 수 있습니다.</p>}
        <div className="citizen-tools-actions"><button type="submit" className="citizen-tools-primary" disabled={busy || accessBlocked || !content.trim() || (mode === "edit" && !editable(selected)) || (mode === "follow-up" && (!available(selected) || selected.complaint_status !== "완료"))}>{busy ? "저장 중..." : mode === "edit" ? "수정 저장" : mode === "follow-up" ? "재민원 접수" : "민원 접수"}</button>{mode !== "create" && <button type="button" disabled={busy} onClick={() => { setMode("detail"); setError(""); setNotice(""); }}>{mode === "edit" ? "수정 취소" : "재민원 작성 취소"}</button>}</div>
        <button type="button" className="citizen-tools-ai" disabled={busy || accessBlocked || recommendationBusy || !content.trim() || !title.trim()} onClick={() => callbacks.current.onRecommend?.(recommendationRecord())}>작성 내용으로 AI 답변 보기</button>
      </form>}
      {mode === "list" && <div className="citizen-tools-list"><div className="citizen-tools-list-heading"><h3>{listKind === "search" ? "검색된 민원" : "내 민원 목록"}</h3><div className="citizen-tools-list-options"><button type="button" aria-expanded={filtersOpen} disabled={busy || (listKind === "search" && search.state === "loading")} onClick={() => setFiltersOpen((current) => !current)}>조회 옵션</button><button type="button" disabled={busy || loading || (listKind === "search" && search.state !== "complete")} onClick={() => { setError(""); if (listKind === "search") refreshSearch(); else loadList(page); }}>새로고침</button></div></div>
        {filtersOpen && <ComplaintFilters initialValue={filterInitial || appliedFilters} onApply={applyFilters} onClear={clearFilters} busy={busy || loading} compact />}
        {listKind === "normal" && <>
          {appliedFilters && <p className="citizen-tools-filter-summary" role="status">조회 조건: {appliedFilters.departments.join(", ")} · {({ unassigned: "미접수", in_progress: "접수 중", completed: "접수 완료" })[appliedFilters.view_status]}</p>}
          {!loading && records.length === 0 && <p className="citizen-tools-muted">작성한 민원이 없습니다.</p>}
          {renderRows(records)}{renderPagination(total, page, setPage)}
        </>}
        {listKind === "search" && <>
          <p className="citizen-tools-search-query">검색 내용: {search.query}</p>
          {search.state === "loading" && <div className="citizen-tools-search-loading" role="status"><span aria-hidden="true" />관련 민원을 검색하고 있습니다.</div>}
          {search.state === "failed" && <p className="citizen-tools-muted">채팅에서 검색을 다시 요청해 주세요.</p>}
          {search.state === "complete" && <>
            <section aria-label="검색된 민원" className="citizen-tools-search-results">{search.complaints.length ? renderRows(search.complaints.slice((page - 1) * pageSize, page * pageSize)) : <p className="citizen-tools-muted">검색 내용에 해당하는 민원이 없습니다.</p>}{renderPagination(search.complaints.length, page, setPage, "검색된 민원")}</section>
            <section aria-label="검색과 유사한 민원" className="citizen-tools-search-similar"><h3>검색과 유사한 민원</h3>{search.similar_complaints.length ? renderRows(search.similar_complaints.slice((similarPage - 1) * pageSize, similarPage * pageSize)) : <p className="citizen-tools-muted">추가로 표시할 유사 민원이 없습니다.</p>}{renderPagination(search.similar_complaints.length, similarPage, setSimilarPage, "검색과 유사한 민원")}</section>
          </>}
        </>}
      </div>}
      {mode === "detail" && selected && <article className="citizen-tools-detail"><div className="citizen-tools-detail-heading"><h3>{selected.title || `민원 #${selected.id}`}</h3><span>#{selected.id} · {citizenViewStatusLabel(selected)}</span></div>
        <p className="citizen-tools-muted">{selected.category || "담당 부서 판별 중"}{selected.analysis_state === "pending" || selected.analysis_state === "processing" ? " · 민원 분석 중" : ""}</p>
        {!available(selected) ? <div className="citizen-tools-cancellation"><strong>민원이 취소되었습니다.</strong><p>{selected.cancelled_by_role === "user" ? "민원인이 해당 민원을 취소했습니다." : selected.cancellation_reason || "취소 사유가 기록되지 않았습니다."}</p></div> : <>
          <h4>민원 내용</h4><p className="citizen-tools-original">{selected.content}</p><PreviousContext context={selected.previous_context} />
          {selected.latest_response && selected.latest_response_state === "sent" && <div className="citizen-tools-answer"><h4>공식 처리 답변</h4><p className="citizen-tools-original">{selected.latest_response}</p></div>}
          {!selected.latest_response && <p className="citizen-tools-muted">아직 전달된 공식 답변이 없습니다.</p>}
          <div className="citizen-tools-actions">{editable(selected) && <button type="button" disabled={busy || accessBlocked} onClick={() => { setError(""); setNotice(""); applyIntent(selected, "edit"); }}>민원 수정</button>}{removable(selected) && <button type="button" className="citizen-tools-danger" disabled={busy || accessBlocked} onClick={() => { setDeleteConfirm(true); setError(""); }}>민원 삭제</button>}{selected.complaint_status === "완료" && <button type="button" disabled={busy || accessBlocked} onClick={startFollowUp}>재민원 작성</button>}</div>
          {selected.complaint_status === "진행중" && <p className="citizen-tools-muted">관리자가 접수를 시작하여 수정할 수 없습니다.</p>}
          <button type="button" className="citizen-tools-ai" disabled={busy || accessBlocked || recommendationBusy} onClick={() => callbacks.current.onRecommend?.(selected)}>이 민원으로 AI 답변 보기</button>
        </>}
        {deleteConfirm && removable(selected) && <section className="citizen-tools-confirm" role="dialog" aria-label="민원 삭제 확인"><h4>이 민원을 삭제할까요?</h4><p>민원이 취소되며 작성한 민원 목록에서 제외됩니다.</p><div className="citizen-tools-actions"><button type="button" className="citizen-tools-danger" disabled={busy} onClick={confirmDelete}>{busy ? "삭제 중..." : "삭제 확인"}</button><button type="button" disabled={busy} onClick={() => setDeleteConfirm(false)}>삭제 취소</button></div></section>}
        <button type="button" className="citizen-tools-back" disabled={busy} onClick={showList}>목록으로 돌아가기</button>
      </article>}
    </div>
  </section>;
}

function PreviousContext({ context, depth = 0 }) {
  if (!context || depth >= 5) return null;
  return <details className="citizen-tools-previous"><summary>이전 민원과 답변</summary><strong>{context.title}</strong><p className="citizen-tools-original">{context.content}</p>{context.response && <><b>이전 공식 답변</b><p className="citizen-tools-original">{context.response}</p></>}<PreviousContext context={context.previous_context} depth={depth + 1} /></details>;
}
