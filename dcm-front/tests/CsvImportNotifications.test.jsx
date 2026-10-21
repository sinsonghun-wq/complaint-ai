import { afterEach, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import App from '../src/App.jsx';

afterEach(() => { cleanup(); sessionStorage.clear(); localStorage.clear(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });
const response = (data) => new Response(JSON.stringify(data), { status: 200 });

it.each([0, 2])('shows completed unreadable count only when positive: %s', async (unreadable) => {
  const user = { role: 'admin', name: '관리자', department_id: 2, department_name: '교통·국토' };
  sessionStorage.setItem('complaintai.fastapi.auth', JSON.stringify({ token: 'test-only', user }));
  const intervals = vi.spyOn(window, 'setInterval');
  vi.stubGlobal('fetch', vi.fn(async (url, options = {}) => {
    const path = new URL(url).pathname;
    if (path === '/api/auth/context') return response({ user });
    if (path === '/api/departments') return response({ departments: [{ id: 2, name: '교통·국토' }] });
    if (path === '/api/complaints/counts') return response({ categories: [{ department_id: 2, count: 2 }] });
    if (path === '/api/complaints') return response({ complaints: [], total: 0 });
    if (path === '/api/imports' && options.method === 'POST') return response({ job_id: 'csv-test', status: 'queued', needs_mapping: false, total_rows: 2 + unreadable });
    if (path === '/api/imports/csv-test') return response({ id: 'csv-test', status: 'completed', total_rows: 2 + unreadable, completed_rows: 2 + unreadable, saved_rows: 2, unreadable_rows: unreadable });
    throw new Error(`Unexpected API: ${path}`);
  }));
  const { container } = render(<App />);
  fireEvent.change(container.querySelector('input[type="file"]'), { target: { files: [new File(['제목,본문\n제목,내용'], 'sample.csv', { type: 'text/csv' })] } });
  fireEvent.click(screen.getByRole('button', { name: '파일 일괄 처리' }));
  await waitFor(() => expect(intervals.mock.calls.some((call) => call[1] === 1000)).toBe(true));
  const poll = intervals.mock.calls.find((call) => call[1] === 1000)[0];
  await act(async () => { await poll(); });
  await screen.findByText(/CSV 처리가 완료되었습니다. 총 2건/);
  expect(screen.getByRole('progressbar').getAttribute('aria-valuenow')).toBe('100');
  if (unreadable) {
    expect(screen.getAllByText(/읽을 수 없는 CSV 항목 2건은 제외했습니다/)).toHaveLength(2);
    fireEvent.click(screen.getByRole('button', { name: '새 민원', exact: true }));
    expect(screen.getByRole('status').textContent).toContain('읽을 수 없는 CSV 항목 2건은 제외했습니다');
  } else {
    expect(screen.queryByText(/읽을 수 없는/)).toBeNull();
    expect(screen.queryByText(/제외했습니다/)).toBeNull();
  }
});
