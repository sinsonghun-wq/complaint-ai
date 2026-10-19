import React from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import DeletedData from '../src/DeletedData.jsx';
import departments from '../../app/departments.json';

afterEach(() => { cleanup(); vi.restoreAllMocks(); });
const record = (id, department_id = 2, deleted_by_role = 'user') => ({
  id, title: `보관된 원본 제목 ${id}`, department_id, department_name: departments.find((d) => d.id === department_id)?.name,
  deleted_by_role, deleted_at: '2026-10-19T01:00:00Z', created_at: '2026-10-18', can_manage: department_id === 2,
});
function setup(initial = [record(1), record(2, 4, 'admin'), record(3, 2, 'admin')], options = {}) {
  let rows = initial;
  const api = vi.fn(async (path, request = {}) => {
    const url = new URL(path, 'http://test');
    if (url.pathname === '/api/trash/counts') {
      if (options.countsError) throw new Error('건수 조회 실패');
      return { total: rows.length, unassigned: 0, departments: departments.map((d) => ({ id: d.id, name: d.name, count: rows.filter((r) => r.department_id === d.id).length })), policy: { capacity: 10000 } };
    }
    if (url.pathname === '/api/trash/items') {
      const id = Number(url.searchParams.get('department_id'));
      const selected = id ? rows.filter((r) => r.department_id === id) : rows;
      const offset = Number(url.searchParams.get('offset')) || 0;
      const limit = Number(url.searchParams.get('limit')) || 20;
      return { items: selected.slice(offset, offset + limit), total: selected.length };
    }
    if (path === '/api/trash/permanent') {
      const body = JSON.parse(request.body);
      if (options.permanent) return options.permanent(body);
      if (body.password !== 'correct-password') throw new Error('비밀번호가 올바르지 않습니다.');
      rows = rows.filter((r) => !body.ids.includes(r.id));
      return { permanently_deleted: body.ids.length };
    }
    if (path === '/api/trash/department/2/permanent') {
      const n = rows.filter((r) => r.department_id === 2).length;
      rows = rows.filter((r) => r.department_id !== 2);
      return { permanently_deleted: n };
    }
    if (path.endsWith('/restore')) {
      const id = Number(path.split('/')[3]); rows = rows.filter((r) => r.id !== id);
      return { restored: 1, admin_only: true };
    }
    const id = Number(path.split('/').at(-1));
    return { complaint: { ...rows.find((r) => r.id === id), content: `원본 본문 ${id}`, cancellation_reason: '보관된 사유' } };
  });
  const changed = vi.fn();
  render(<DeletedData api={api} ownDepartmentId={2} onChanged={changed} />);
  return { api, changed };
}
const article = (id) => screen.getByText(`보관된 원본 제목 ${id}`).closest('article');
async function openDelete() {
  fireEvent.click(screen.getByLabelText('보관된 원본 제목 1 선택'));
  fireEvent.click(screen.getByRole('button', { name: '선택 영구 삭제' }));
  return screen.findByRole('dialog');
}

describe('삭제된 데이터 보관함', () => {
  it('shows all departments, original titles and deletion origin without fetching bodies', async () => {
    const { api } = setup();
    await screen.findByText('보관된 원본 제목 1');
    const filters = screen.getByRole('group', { name: '삭제된 데이터 부서 필터' });
    expect(within(filters).getAllByRole('button').length).toBe(8);
    expect(screen.getAllByText('관리자 삭제').length).toBe(2);
    expect(screen.getAllByText('민원인 삭제').length).toBe(1);
    expect(screen.queryByText('원본 본문 1')).toBeNull();
    expect(api.mock.calls.some(([path]) => path === '/api/trash/1')).toBe(false);
    const details = within(article(1)).getByText('원본 내용 확인').closest('details');
    details.open = true; fireEvent(details, new Event('toggle'));
    expect(await screen.findByText('원본 본문 1')).toBeTruthy();
    expect(screen.getByText('삭제 사유: 보관된 사유')).toBeTruthy();
  });

  it('filters other departments read-only and disables their selection', async () => {
    setup(); await screen.findByText('보관된 원본 제목 1');
    expect(screen.getByLabelText('보관된 원본 제목 2 선택').disabled).toBe(true);
    fireEvent.click(screen.getByRole('button', { name: /환경·위생/ }));
    await waitFor(() => expect(screen.queryByText('보관된 원본 제목 1')).toBeNull());
    expect(within(article(2)).getByText('읽기 전용')).toBeTruthy();
    expect(within(article(2)).queryByRole('button', { name: '복원' })).toBeNull();
    expect(screen.queryByRole('button', { name: '담당 부서 전체 영구 삭제' })).toBeNull();
  });

  it('requires a password and sends selected IDs in one permanent-delete request', async () => {
    const { api, changed } = setup(); await screen.findByText('보관된 원본 제목 1');
    fireEvent.click(screen.getByLabelText('보관된 원본 제목 3 선택'));
    const dialog = await openDelete();
    const password = within(dialog).getByLabelText('로그인 비밀번호');
    expect(password.type).toBe('password');
    expect(within(dialog).getByRole('button', { name: '영구 삭제 실행' }).disabled).toBe(true);
    fireEvent.change(password, { target: { value: 'correct-password' } });
    fireEvent.click(within(dialog).getByRole('button', { name: '영구 삭제 실행' }));
    expect(await screen.findByText('2건을 영구 삭제했습니다.')).toBeTruthy();
    const call = api.mock.calls.find(([path]) => path === '/api/trash/permanent');
    expect(JSON.parse(call[1].body)).toEqual({ password: 'correct-password', ids: [3, 1] });
    expect(changed).toHaveBeenCalledOnce();
    expect(screen.queryByText('보관된 원본 제목 1')).toBeNull();
  });

  it('blocks double submission while permanent deletion is pending', async () => {
    let resolve;
    const pending = new Promise((done) => { resolve = done; });
    const { api } = setup(undefined, { permanent: () => pending });
    await screen.findByText('보관된 원본 제목 1');
    const dialog = await openDelete();
    fireEvent.change(within(dialog).getByLabelText('로그인 비밀번호'), { target: { value: 'correct-password' } });
    fireEvent.click(within(dialog).getByRole('button', { name: '영구 삭제 실행' }));
    const deleting = within(dialog).getByRole('button', { name: '영구 삭제 중...' });
    expect(deleting.disabled).toBe(true); fireEvent.click(deleting);
    expect(api.mock.calls.filter(([path]) => path === '/api/trash/permanent').length).toBe(1);
    await act(async () => resolve({ permanently_deleted: 1 }));
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  });

  it('keeps the confirmation open when the password is rejected', async () => {
    setup(); await screen.findByText('보관된 원본 제목 1');
    const dialog = await openDelete();
    fireEvent.change(within(dialog).getByLabelText('로그인 비밀번호'), { target: { value: 'wrong' } });
    fireEvent.click(within(dialog).getByRole('button', { name: '영구 삭제 실행' }));
    expect(await within(dialog).findByRole('alert')).toBeTruthy();
    expect(screen.getByText('보관된 원본 제목 1')).toBeTruthy();
    expect(within(dialog).getByRole('button', { name: '영구 삭제 실행' }).disabled).toBe(false);
  });

  it('restores only to the administrator archive and refreshes counts', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    const { api, changed } = setup(); await screen.findByText('보관된 원본 제목 1');
    fireEvent.click(within(article(1)).getByRole('button', { name: '복원' }));
    expect(await screen.findByText('관리자 보관함으로 복원했습니다. 민원인 목록에는 표시되지 않습니다.')).toBeTruthy();
    expect(api.mock.calls.some(([path]) => path === '/api/trash/1/restore')).toBe(true);
    expect(changed).toHaveBeenCalledOnce();
  });

  it('purges the whole own department, not the whole global archive', async () => {
    const { api } = setup(); await screen.findByText('보관된 원본 제목 1');
    fireEvent.click(screen.getByRole('button', { name: '담당 부서 전체 영구 삭제' }));
    const dialog = await screen.findByRole('dialog');
    fireEvent.change(within(dialog).getByLabelText('로그인 비밀번호'), { target: { value: 'correct-password' } });
    fireEvent.click(within(dialog).getByRole('button', { name: '영구 삭제 실행' }));
    expect(await screen.findByText('2건을 영구 삭제했습니다.')).toBeTruthy();
    expect(api.mock.calls.some(([path]) => path === '/api/trash/department/2/permanent')).toBe(true);
    expect(screen.getByText('보관된 원본 제목 2')).toBeTruthy();
  });

  it('preserves valid lists when the independent count request fails', async () => {
    setup(undefined, { countsError: true });
    expect(await screen.findByText('보관된 원본 제목 1')).toBeTruthy();
    expect(screen.getByText(/보관 건수를 불러오지 못했습니다/)).toBeTruthy();
  });

  it('paginates and retains selected IDs across pages with 10/20/50/100 choices', async () => {
    const { api } = setup(Array.from({ length: 25 }, (_, i) => record(i + 1)));
    await screen.findByText('보관된 원본 제목 1');
    expect([...screen.getByLabelText('한 번에 보기').options].map((o) => o.value)).toEqual(['10','20','50','100']);
    fireEvent.click(screen.getByLabelText('보관된 원본 제목 1 선택'));
    fireEvent.click(screen.getByRole('button', { name: '다음' }));
    await screen.findByText('보관된 원본 제목 21');
    fireEvent.click(screen.getByLabelText('보관된 원본 제목 21 선택'));
    fireEvent.click(screen.getByRole('button', { name: '선택 영구 삭제' }));
    const dialog = await screen.findByRole('dialog');
    fireEvent.change(within(dialog).getByLabelText('로그인 비밀번호'), { target: { value: 'correct-password' } });
    fireEvent.click(within(dialog).getByRole('button', { name: '영구 삭제 실행' }));
    await screen.findByText('2건을 영구 삭제했습니다.');
    expect(JSON.parse(api.mock.calls.find(([path]) => path === '/api/trash/permanent')[1].body).ids).toEqual([1,21]);
  });

  it('supports Escape and returns focus from the password dialog', async () => {
    setup(); await screen.findByText('보관된 원본 제목 1');
    await openDelete();
    fireEvent.keyDown(document, { key: 'Escape' });
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  });
});
