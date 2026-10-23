import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import App from '../src/App.jsx';

afterEach(() => { cleanup(); sessionStorage.clear(); localStorage.clear(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });
const buttonName = '부서 민원 전체 삭제';
const defaultReason = '해당 민원은 잘못된 사유로 인해 취소되었습니다.';
const response = (body, status = 200) => new Response(JSON.stringify(body), { status });
const deletes = (calls) => calls.filter((call) => call.method === 'DELETE');

function setup({ allFiles = false, empty = false } = {}) {
  const user = { role: 'admin', name: '관리자', department_id: 2, department_name: '교통·국토' };
  sessionStorage.setItem('complaintai.fastapi.auth', JSON.stringify({ token: 'test-only', user }));
  let rows = empty ? [] : Array.from({ length: 12 }, (_, index) => ({
    id: index + 1, title: `민원 ${index + 1}`, content: '원본 내용', summary: '요약 내용',
    complaint_status: ['접수', '진행중', '완료', '취소'][index % 4],
    submitted_by_user: !allFiles && index % 2 === 0, can_manage: true,
    category: '교통·국토', department_id: 2, department_name: '교통·국토', created_at: '2026-10-20',
  }));
  let trash = [];
  const control = { calls: [], pausedReads: [], pauseReads: false, holdDelete: false, failDelete: false, failRefresh: false };
  // This flow must work even when the browser cannot show native JS dialogs.
  vi.stubGlobal('confirm', vi.fn(() => { throw new Error('Native confirm is unavailable'); }));
  vi.stubGlobal('prompt', vi.fn(() => { throw new Error('Native prompt is unavailable'); }));
  vi.stubGlobal('fetch', vi.fn(async (url, options = {}) => {
    const location = new URL(url);
    const path = location.pathname;
    control.calls.push({ path, search: location.search, ...options });
    if (path === '/api/auth/context') return response({ user });
    if (path === '/api/departments') return response({ departments: [{ id: 2, name: '교통·국토' }] });
    if (path === '/api/complaints/counts' || path === '/api/complaints') {
      if (control.failRefresh && trash.length) return response({ detail: '조회 오류' }, 500);
      let result;
      if (path.endsWith('/counts')) result = response({ categories: rows.length ? [{ department_id: 2, count: rows.length }] : [] });
      else {
        const source = location.searchParams.get('source');
        const filtered = rows.filter((row) => source === 'user' ? row.submitted_by_user : source === 'file' ? !row.submitted_by_user : true);
        const offset = Number(location.searchParams.get('offset')) || 0;
        const limit = Number(location.searchParams.get('limit')) || 10;
        result = response({ complaints: filtered.slice(offset, offset + limit), total: filtered.length });
      }
      if (control.pauseReads) return new Promise((resolve) => control.pausedReads.push(() => resolve(result)));
      return result;
    }
    if (path === '/api/departments/2/complaints' && options.method === 'DELETE') {
      if (control.holdDelete) await new Promise((resolve) => { control.finishDelete = resolve; });
      if (control.failDelete) return response({ detail: '이동 처리에 실패했습니다.' }, 500);
      const reason = JSON.parse(options.body).reason;
      trash = rows.map((row) => ({ ...row, deleted_at: '2026-10-20T05:30:00Z', deleted_by_role: 'admin', cancellation_reason: reason }));
      rows = [];
      return response({ deleted: trash.length, automatically_purged: 0 });
    }
    if (path === '/api/trash/items') return response({ items: trash, total: trash.length });
    if (path === '/api/trash/counts') return response({ total: trash.length, departments: [{ id: 2, name: '교통·국토', count: trash.length }], unassigned: 0 });
    if (/^\/api\/trash\/\d+$/.test(path)) return response({ complaint: trash.find((row) => row.id === Number(path.split('/').at(-1))) });
    throw new Error(`Unexpected request: ${path}`);
  }));
  return control;
}

async function openList() {
  render(<App />);
  fireEvent.click(screen.getByRole('button', { name: '분류 목록 · 민원 접수', exact: true }));
  await screen.findByRole('button', { name: /12교통·국토/ });
}
function openWarning() {
  const opener = screen.getByRole('button', { name: buttonName });
  opener.focus(); fireEvent.click(opener);
  return screen.getByRole('dialog', { name: '부서 민원 전체 삭제 확인' });
}
function confirmMove(dialog) {
  fireEvent.click(within(dialog).getByRole('button', { name: '삭제된 데이터로 이동' }));
}

describe('부서 민원 전체 삭제', () => {
  it('preserves the warning and moves every status, source and page with the default reason', async () => {
    const { calls } = setup();
    await openList();
    await screen.findByText('민원 1');
    expect(screen.queryByText('민원 11')).toBeNull();
    const dialog = openWarning();
    expect(within(dialog).getByText('교통·국토 부서의 전체 민원을 삭제합니다. 출처 필터와 관계없이 적용됩니다. 정말로 전체 삭제하시겠습니까?')).toBeTruthy();
    expect(within(dialog).getByText(/접수 대기·접수 됨·완료·취소/)).toBeTruthy();
    expect(within(dialog).getByText(`기본 삭제 사유: ${defaultReason}`)).toBeTruthy();
    expect(within(dialog).queryByRole('textbox')).toBeNull();
    expect(deletes(calls)).toHaveLength(0);
    confirmMove(dialog);
    await screen.findByText('교통·국토 부서 민원 12건을 삭제 보관함으로 이동했습니다.');
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(screen.queryByText('민원 1')).toBeNull();
    expect(screen.getByText('표시할 민원이 없습니다.')).toBeTruthy();
    expect(screen.getByRole('button', { name: /0교통·국토/ })).toBeTruthy();
    expect(screen.getByText('0건')).toBeTruthy();
    expect(screen.getByRole('button', { name: buttonName }).disabled).toBe(true);
    expect(window.confirm).not.toHaveBeenCalled();
    expect(window.prompt).not.toHaveBeenCalled();
    expect(deletes(calls)).toHaveLength(1);
    expect(deletes(calls)[0].path).toBe('/api/departments/2/complaints');
    expect(deletes(calls)[0].search).toBe('');
    expect(JSON.parse(deletes(calls)[0].body)).toEqual({ reason: defaultReason });
    fireEvent.click(screen.getByRole('button', { name: '삭제된 데이터', exact: true }));
    await screen.findByText('민원 12');
    expect(screen.getAllByText(/^민원 \d+$/)).toHaveLength(12);
    expect(screen.getByRole('button', { name: /12교통·국토/ })).toBeTruthy();
    fireEvent.click(screen.getAllByText('원본 내용 확인')[0]);
    await screen.findByText(`삭제 사유: ${defaultReason}`);
    fireEvent.click(screen.getByRole('button', { name: '분류 목록 · 민원 접수', exact: true }));
    await screen.findByText('표시할 민원이 없습니다.');
    expect(screen.queryByText('민원 1')).toBeNull();
  });

  it('works when the selected source filter displays zero rows', async () => {
    const { calls } = setup({ allFiles: true });
    await openList();
    fireEvent.click(screen.getByRole('button', { name: '민원인 작성', exact: true }));
    await screen.findByText('표시할 민원이 없습니다.');
    expect(screen.getByRole('button', { name: buttonName }).disabled).toBe(false);
    confirmMove(openWarning());
    await screen.findByText('교통·국토 부서 민원 12건을 삭제 보관함으로 이동했습니다.');
    expect(deletes(calls)).toHaveLength(1);
  });

  it.each(['button', 'Escape'])('does not submit when the warning is dismissed with %s', async (method) => {
    const { calls } = setup();
    await openList();
    const opener = screen.getByRole('button', { name: buttonName });
    const dialog = openWarning();
    expect(document.activeElement).toBe(within(dialog).getByRole('button', { name: '취소' }));
    if (method === 'button') fireEvent.click(within(dialog).getByRole('button', { name: '취소' }));
    else fireEvent.keyDown(dialog, { key: 'Escape' });
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(document.activeElement).toBe(opener);
    expect(deletes(calls)).toHaveLength(0);
    expect(screen.getByText('민원 1')).toBeTruthy();
  });

  it('ignores late list/count responses from before the move', async () => {
    const control = setup();
    await openList();
    await screen.findByText('민원 1');
    control.pauseReads = true;
    fireEvent.click(screen.getByRole('button', { name: '파일 업로드', exact: true }));
    await waitFor(() => expect(control.pausedReads).toHaveLength(2));
    control.pauseReads = false;
    confirmMove(openWarning());
    await screen.findByText('교통·국토 부서 민원 12건을 삭제 보관함으로 이동했습니다.');
    await act(async () => { control.pausedReads.forEach((resolve) => resolve()); });
    expect(screen.queryByText('민원 2')).toBeNull();
    expect(screen.getByText('표시할 민원이 없습니다.')).toBeTruthy();
    expect(screen.getByRole('button', { name: /0교통·국토/ })).toBeTruthy();
  });

  it('pauses polling and prevents duplicate submission while the move is pending', async () => {
    const intervals = vi.spyOn(window, 'setInterval');
    const control = setup();
    control.holdDelete = true;
    await openList();
    const poll = intervals.mock.calls.find((call) => call[1] === 3000)[0];
    const dialog = openWarning();
    const submit = within(dialog).getByRole('button', { name: '삭제된 데이터로 이동' });
    fireEvent.click(submit); fireEvent.click(submit);
    expect(deletes(control.calls)).toHaveLength(1);
    expect(submit.disabled).toBe(true);
    expect(within(dialog).getByRole('button', { name: '취소' }).disabled).toBe(true);
    const beforePoll = control.calls.length;
    await act(async () => { await poll(); });
    expect(control.calls).toHaveLength(beforePoll);
    fireEvent.keyDown(dialog, { key: 'Escape' });
    expect(screen.getByRole('dialog')).toBeTruthy();
    await act(async () => control.finishDelete());
    await screen.findByText('교통·국토 부서 민원 12건을 삭제 보관함으로 이동했습니다.');
  });

  it('keeps the confirmed UI move even if subsequent refresh requests fail', async () => {
    const control = setup();
    control.failRefresh = true;
    await openList();
    confirmMove(openWarning());
    await screen.findByText(/교통·국토 부서 민원 12건을 삭제 보관함으로 이동했습니다. 목록 재확인에 실패했습니다./);
    expect(screen.queryByText('민원 1')).toBeNull();
    expect(screen.getByText('표시할 민원이 없습니다.')).toBeTruthy();
    expect(screen.getByRole('button', { name: /0교통·국토/ })).toBeTruthy();
  });

  it('shows an error, preserves the list and allows retry if the move fails', async () => {
    const control = setup();
    control.failDelete = true;
    await openList();
    const dialog = openWarning();
    confirmMove(dialog);
    await within(dialog).findByRole('alert');
    expect(within(dialog).getByRole('alert').textContent).toBe('이동 처리에 실패했습니다.');
    expect(screen.getByText('민원 1')).toBeTruthy();
    expect(screen.getByRole('button', { name: /12교통·국토/ })).toBeTruthy();
    control.failDelete = false;
    confirmMove(dialog);
    await screen.findByText('교통·국토 부서 민원 12건을 삭제 보관함으로 이동했습니다.');
  });

  it('disables the action when the entire department has no complaints', async () => {
    setup({ empty: true });
    render(<App />);
    fireEvent.click(screen.getByRole('button', { name: '분류 목록 · 민원 접수', exact: true }));
    await screen.findByRole('button', { name: /0교통·국토/ });
    await waitFor(() => expect(screen.getByRole('button', { name: buttonName }).disabled).toBe(true));
  });
});
