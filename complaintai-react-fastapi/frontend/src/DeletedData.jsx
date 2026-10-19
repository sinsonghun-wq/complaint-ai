import { useCallback, useEffect, useRef, useState } from "react";
import "./deletedData.css";

const originName = (role) => ({ admin: "관리자 삭제", user: "민원인 삭제", unknown: "삭제 주체 기록 없음" }[role] || "삭제 주체 기록 없음");

export default function DeletedData({ api, ownDepartmentId, onChanged }) {
  const [filter, setFilter] = useState("all");
  const [page, setPage] = useState(1);
  const [size, setSize] = useState(20);
  const [data, setData] = useState({ items: [], total: 0 });
  const [stats, setStats] = useState(null);
  const [selected, setSelected] = useState(new Set());
  const [details, setDetails] = useState({});
  const [error, setError] = useState("");
  const [countsError, setCountsError] = useState("");
  const [notice, setNotice] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(null);
  const [pending, setPending] = useState(null);
  const [password, setPassword] = useState("");
  const [dialogError, setDialogError] = useState("");
  const sequence = useRef(0);
  const actionLock = useRef(false);
  const detailPending = useRef(new Set());
  const detailScope = useRef(0);
  const dialog = useRef(null);
  const opener = useRef(null);

  const load = useCallback(async () => {
    const request = ++sequence.current;
    const params = new URLSearchParams({ limit: String(size), offset: String((page - 1) * size) });
    if (filter === "unassigned") params.set("unassigned", "true");
    else if (filter !== "all") params.set("department_id", filter);
    const results = await Promise.allSettled([
      api(`/api/trash/items?${params}`).then((result) => {
        if (request !== sequence.current) return;
        if (!Array.isArray(result.items) || !Number.isFinite(result.total)) throw new Error("삭제 보관함 응답 형식을 확인하지 못했습니다.");
        setData(result); setError("");
        if ((page - 1) * size >= result.total && page > 1) setPage(Math.max(1, Math.ceil(result.total / size)));
      }),
      api("/api/trash/counts").then((result) => {
        if (!Array.isArray(result.departments) || !Number.isFinite(result.total)) throw new Error("삭제 보관함 건수 응답 형식을 확인하지 못했습니다.");
        if (request === sequence.current) { setStats(result); setCountsError(""); }
      }).catch((failure) => { if (request === sequence.current) setCountsError(failure.message); }),
    ]);
    if (request === sequence.current) {
      if (results[0].status === "rejected") setError(results[0].reason.message);
      setLoading(false);
    }
  }, [api, filter, page, size]);

  useEffect(() => {
    setLoading(true); load();
    const timer = window.setInterval(load, 5000);
    return () => { sequence.current++; window.clearInterval(timer); };
  }, [load]);
  useEffect(() => { detailScope.current++; setDetails({}); }, [filter, page, size]);
  useEffect(() => {
    if (!pending) return;
    const keyboard = (event) => {
      if (event.key === "Escape" && !actionLock.current) { setPending(null); setPassword(""); }
      if (event.key === "Tab") {
        const controls = [...(dialog.current?.querySelectorAll("input:not(:disabled),button:not(:disabled)") || [])];
        const first = controls[0], last = controls.at(-1);
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
      }
    };
    document.addEventListener("keydown", keyboard);
    return () => { document.removeEventListener("keydown", keyboard); opener.current?.focus?.(); };
  }, [pending]);

  const chooseFilter = (next) => { setFilter(next); setPage(1); setSelected(new Set()); setNotice(""); };
  const toggle = (id) => setSelected((current) => {
    const next = new Set(current);
    next.has(id) ? next.delete(id) : next.size < 1000 && next.add(id);
    return next;
  });
  const manageable = data.items.filter((item) => item.can_manage);
  const allPageSelected = manageable.length > 0 && manageable.every((item) => selected.has(item.id));
  const togglePage = () => setSelected((current) => {
    const next = new Set(current);
    manageable.forEach((item) => allPageSelected ? next.delete(item.id) : next.size < 1000 && next.add(item.id));
    return next;
  });
  const readOriginal = async (item, open) => {
    if (!open || details[item.id]?.record || detailPending.current.has(item.id)) return;
    detailPending.current.add(item.id);
    const scope = detailScope.current;
    setDetails((current) => ({ ...current, [item.id]: { loading: true } }));
    try {
      const result = await api(`/api/trash/${item.id}`);
      if (scope === detailScope.current) setDetails((current) => ({ ...current, [item.id]: { record: result.complaint } }));
    } catch (failure) {
      if (scope === detailScope.current) setDetails((current) => ({ ...current, [item.id]: { error: failure.message } }));
    } finally { detailPending.current.delete(item.id); }
  };
  const restore = async (item) => {
    if (actionLock.current || !window.confirm("관리자 보관함으로만 복원합니다. 민원인 목록에는 다시 나타나지 않습니다. 복원할까요?")) return;
    actionLock.current = true; setBusy(`restore-${item.id}`); setError("");
    try {
      const result = await api(`/api/trash/${item.id}/restore`, { method: "POST" });
      if (result.restored !== 1 || !result.admin_only) throw new Error("복원 결과를 확인하지 못했습니다.");
      setSelected(new Set()); await load(); await onChanged?.();
      setNotice("관리자 보관함으로 복원했습니다. 민원인 목록에는 표시되지 않습니다.");
    } catch (failure) { setError(failure.message); }
    finally { actionLock.current = false; setBusy(null); }
  };
  const askDelete = (action) => {
    if (actionLock.current) return;
    opener.current = document.activeElement;
    setPending(action); setPassword(""); setDialogError("");
  };
  const permanentlyDelete = async (event) => {
    event.preventDefault();
    if (actionLock.current || !password) return;
    actionLock.current = true; setBusy("permanent"); setDialogError("");
    try {
      const path = pending.kind === "department" ? `/api/trash/department/${pending.departmentId}/permanent` : "/api/trash/permanent";
      const body = pending.kind === "department" ? { password } : { password, ids: pending.ids };
      const result = await api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      setPending(null); setPassword(""); setSelected(new Set()); setDetails({});
      await load(); await onChanged?.();
      setNotice(`${result.permanently_deleted.toLocaleString()}건을 영구 삭제했습니다.`);
    } catch (failure) { setDialogError(failure.message); }
    finally { actionLock.current = false; setBusy(null); }
  };
  const ownDepartment = stats?.departments?.find((d) => d.id === ownDepartmentId);
  const isOwnFilter = filter === "all" || Number(filter) === ownDepartmentId;

  return <div className="deleted-data">
    <section className="panel trash-overview">
      <div className="list-head"><h2>삭제된 데이터 보관함</h2><p>{(stats?.total || 0).toLocaleString()} / {(stats?.policy?.capacity || 10000).toLocaleString()}건</p></div>
      <p className="muted">관리자 또는 민원인이 삭제한 민원을 보관합니다. 다른 부서는 조회만 가능하며, 복원한 민원은 관리자에게만 표시됩니다.</p>
      {countsError && <p role="status">보관 건수를 불러오지 못했습니다. 목록은 계속 사용할 수 있습니다. ({countsError})</p>}
      <div className="grid trash-department-grid" role="group" aria-label="삭제된 데이터 부서 필터">
        <button aria-pressed={filter === "all"} disabled={Boolean(busy)} className={filter === "all" ? "selected" : ""} onClick={() => chooseFilter("all")}><b>{(stats?.total || 0).toLocaleString()}</b><span>전체 부서</span></button>
        {(stats?.departments || []).map((d) => <button key={d.id} aria-pressed={filter === String(d.id)} disabled={Boolean(busy)} className={`${filter === String(d.id) ? "selected" : ""} ${d.id === ownDepartmentId ? "department-owned" : ""}`} onClick={() => chooseFilter(String(d.id))}><b>{d.count.toLocaleString()}</b><span>{d.name}</span></button>)}
        {stats?.unassigned > 0 && <button disabled={Boolean(busy)} className={filter === "unassigned" ? "selected" : ""} aria-pressed={filter === "unassigned"} onClick={() => chooseFilter("unassigned")}><b>{stats.unassigned.toLocaleString()}</b><span>미분류</span></button>}
      </div>
      <details className="trash-policy"><summary>보관 한도와 자동 정리 기준</summary><p>최대 10,000건을 보관합니다. 초과할 때 기존 보관 데이터 중 <b>삭제 보관함에 최근 들어온 순</b>으로 정리합니다. 100건 이하 유입 시 기존 100건, 100건 초과 유입 시 유입 건수에 100건을 더해 영구 삭제합니다. 한 번에 한도보다 많이 들어오면 새로 들어온 데이터도 같은 기준으로 정리될 수 있습니다. 자동 정리는 복구할 수 없습니다.</p></details>
    </section>
    <section className="panel trash-list-panel">
      <div className="list-head"><h2>보관 목록</h2><label>한 번에 보기<select value={size} disabled={Boolean(busy)} onChange={(event) => { setSize(Number(event.target.value)); setPage(1); }}>{[10,20,50,100].map((value) => <option key={value}>{value}</option>)}</select></label></div>
      {error && <p className="notice" role="alert">{error}</p>}
      {notice && <p className="notice" role="status">{notice}</p>}
      <div className="trash-selection-toolbar">
        <label><input type="checkbox" aria-label="현재 페이지 선택" checked={allPageSelected} disabled={Boolean(busy) || manageable.length === 0} onChange={togglePage} /> 현재 페이지 선택</label>
        <span className="trash-selection-count">{selected.size}건 선택 · 한 번에 최대 1,000건</span>
        <button className="danger" disabled={Boolean(busy) || selected.size === 0} onClick={() => askDelete({ kind: "selection", ids: [...selected], count: selected.size })}>선택 영구 삭제</button>
        {isOwnFilter && <button className="danger" disabled={Boolean(busy) || !ownDepartment?.count} onClick={() => askDelete({ kind: "department", departmentId: ownDepartmentId, count: ownDepartment.count })}>담당 부서 전체 영구 삭제</button>}
        {selected.size > 0 && <button disabled={Boolean(busy)} onClick={() => setSelected(new Set())}>선택 해제</button>}
      </div>
      {loading && <p role="status">보관함을 불러오는 중입니다.</p>}
      {!loading && data.items.length === 0 && <p>표시할 삭제된 데이터가 없습니다.</p>}
      <div className="trash-records">{data.items.map((item) => <article key={item.id} className="trash-record">
        <div className="trash-record-heading">
          <label className="trash-record-title"><input type="checkbox" aria-label={`${item.title} 선택`} disabled={Boolean(busy) || !item.can_manage || (selected.size >= 1000 && !selected.has(item.id))} checked={selected.has(item.id)} onChange={() => toggle(item.id)} /><strong>{item.title}</strong></label>
          <div className="trash-record-actions">{item.can_manage ? <><button className="trash-restore" disabled={Boolean(busy)} onClick={() => restore(item)}>{busy === `restore-${item.id}` ? "복원 중..." : "복원"}</button><button className="danger" disabled={Boolean(busy)} onClick={() => askDelete({ kind: "selection", ids: [item.id], count: 1 })}>영구 삭제</button></> : <span className="read-only">읽기 전용</span>}</div>
        </div>
        <p className="trash-record-meta"><span className={`trash-origin trash-origin-${item.deleted_by_role}`}>{originName(item.deleted_by_role)}</span><span>{item.department_name || "미분류"}</span><time dateTime={item.deleted_at}>{new Date(item.deleted_at).toLocaleString()}</time></p>
        <details className="trash-original" onToggle={(event) => readOriginal(item, event.currentTarget.open)}>
          <summary>원본 내용 확인</summary>
          {details[item.id]?.loading && <p role="status">원본 내용을 불러오는 중입니다.</p>}
          {details[item.id]?.error && <p role="alert">{details[item.id].error} <button onClick={() => readOriginal(item, true)}>다시 시도</button></p>}
          {details[item.id]?.record && <><p className="trash-original-body">{details[item.id].record.content || "저장된 원본 내용이 없습니다."}</p>{details[item.id].record.cancellation_reason && <p className="muted">삭제 사유: {details[item.id].record.cancellation_reason}</p>}</>}
        </details>
      </article>)}</div>
      <footer><span>{data.total.toLocaleString()}건</span><button disabled={Boolean(busy) || page <= 1} onClick={() => setPage(page - 1)}>이전</button><span>{page} / {Math.max(1, Math.ceil(data.total / size))}</span><button disabled={Boolean(busy) || page * size >= data.total} onClick={() => setPage(page + 1)}>다음</button></footer>
    </section>
    {pending && <div className="trash-modal-backdrop"><section ref={dialog} className="trash-modal" role="dialog" aria-modal="true" aria-labelledby="trash-delete-title">
      <h2 id="trash-delete-title">영구 삭제 확인</h2>
      <p>{pending.kind === "department" ? `${ownDepartment?.name || "담당 부서"}의 삭제된 데이터 전체` : `선택한 데이터 ${pending.count}건`}를 영구 삭제합니다. 이 작업은 되돌릴 수 없습니다.</p>
      <form onSubmit={permanentlyDelete}><label>로그인 비밀번호<input autoFocus type="password" autoComplete="current-password" required maxLength={256} disabled={Boolean(busy)} value={password} onChange={(event) => setPassword(event.target.value)} /></label>
        {dialogError && <p role="alert">{dialogError}</p>}
        <div className="actions"><button type="button" disabled={Boolean(busy)} onClick={() => { setPending(null); setPassword(""); }}>취소</button><button className="danger" disabled={Boolean(busy) || !password} type="submit">{busy === "permanent" ? "영구 삭제 중..." : "영구 삭제 실행"}</button></div>
      </form>
    </section></div>}
  </div>;
}
