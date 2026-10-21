import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import App from '../src/App.jsx';
import departments from '../src/adminDepartments.json';

afterEach(() => { cleanup(); sessionStorage.clear(); localStorage.clear(); vi.unstubAllGlobals(); });
const response = (body, status = 200) => new Response(JSON.stringify(body), { status });

function setup(departmentId, catalog = departments) {
  const user = { role: 'admin', name: '권한 테스트 관리자', department_id: departmentId, department_name: null };
  sessionStorage.setItem('complaintai.fastapi.auth', JSON.stringify({ token: 'test', user: { ...user, department: '잘못된 과거 부서명' } }));
  const calls = [];
  const record = (id) => ({
    id: id * 10, department_id: id, department_name: catalog.find((d) => d.id === id).name,
    category: departments.find((d) => d.id === id).name, title: '민원 ' + id, content: '민원 원문',
    summary: '요약', complaint_status: '접수', can_manage: id === departmentId,
    submitted_by_user: true, created_at: '2026-10-16', latest_response: '', latest_response_state: '',
  });
  vi.stubGlobal('fetch', vi.fn(async (url, options = {}) => {
    const location = new URL(url); const path = location.pathname;
    calls.push({ path, search: location.search, ...options });
    if (path === '/api/auth/context') return response({ user });
    if (path === '/api/departments') return response({ departments: catalog });
    if (path.endsWith('/counts')) return response({ categories: catalog.map((d) => ({ department_id: d.id, department_name: d.name, count: 1 })) });
    if (path === '/api/complaints') {
      const id = Number(location.searchParams.get('department_id')) || departmentId;
      return response({ complaints: [record(id)], total: 1 });
    }
    if (path.endsWith('/transfer')) return response({ complaint: record(JSON.parse(options.body).department_id) });
    if (path.endsWith('/responses')) return response({ complaint_status: '진행중', response: { content: JSON.parse(options.body).content } });
    return response({ complaint: record(departmentId) });
  }));
  return calls;
}

describe('부서 ID 기반 화면 권한', () => {
  it.each(departments)('$id $name: own department editable, others read-only', async (department) => {
    setup(department.id); render(<App />);
    fireEvent.click(screen.getByRole('button', { name: '분류 목록', exact: true }));
    const own = await screen.findByRole('button', { name: new RegExp(department.name) });
    expect(own.className).toContain('department-owned');
    expect((await screen.findByText('민원 ' + department.id)).closest('article').querySelector('.record-actions button')).toBeTruthy();
    for (const other of departments.filter((d) => d.id !== department.id)) {
      fireEvent.click(screen.getByRole('button', { name: new RegExp(other.name) }));
      const article = (await screen.findByText('민원 ' + other.id)).closest('article');
      expect(within(article).getByText('읽기 전용')).toBeTruthy();
      expect(within(article).queryByRole('button')).toBeNull();
      expect(screen.queryByRole('button', { name: '부서 민원 전체 삭제' })).toBeNull();
    }
    fireEvent.click(own);
    const article = (await screen.findByText('민원 ' + department.id)).closest('article');
    fireEvent.click(within(article).getByRole('button', { name: '민원 접수' }));
    expect(await screen.findByLabelText('답변 내용')).toBeTruthy();
    expect(screen.queryByRole('button', { name: /전체 삭제 \(테스트\)/ })).toBeNull();
  });

  it('loads displayed names from the server catalog and transfers numeric IDs', async () => {
    const catalog = departments.map((d) => ({ ...d, name: d.id === 4 ? 'DB 환경 부서명' : d.name }));
    const calls = setup(2, catalog); render(<App />);
    fireEvent.click(screen.getByRole('button', { name: '분류 목록', exact: true }));
    expect(await screen.findByRole('button', { name: /DB 환경 부서명/ })).toBeTruthy();
    const article = (await screen.findByText('민원 2')).closest('article');
    fireEvent.click(within(article).getByRole('button', { name: '민원 접수' }));
    const target = await screen.findByLabelText('전달할 부서');
    expect(within(target).queryByRole('option', { name: '교통·국토' })).toBeNull();
    fireEvent.change(target, { target: { value: '4' } });
    fireEvent.click(screen.getByRole('button', { name: '부서 전달', exact: true }));
    await waitFor(() => expect(calls.some((call) => call.path.endsWith('/transfer'))).toBe(true));
    expect(JSON.parse(calls.find((call) => call.path.endsWith('/transfer')).body)).toEqual({ department_id: 4 });
    expect(await screen.findByText('DB 환경 부서명 부서로 전달했습니다.')).toBeTruthy();
  });
});
