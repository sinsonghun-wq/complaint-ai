import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import App from '../src/App.jsx';

afterEach(() => { cleanup(); sessionStorage.clear(); localStorage.clear(); vi.unstubAllGlobals(); });
const baseRecord = {
  id: 77, title: '교통 민원 테스트', content: '도로 보수를 요청합니다.', summary: '도로 보수 요청',
  category: '교통·국토', complaint_status: '진행중', can_manage: true, submitted_by_user: true,
  latest_response: '처음 저장한 답변', latest_response_state: 'draft', created_at: '2026-10-13',
};
const jsonResponse = (body, status = 200) => new Response(JSON.stringify(body), { status });
function login(role = 'admin') {
  sessionStorage.setItem('complaintai.fastapi.auth', JSON.stringify({ token: 'test-only', user: { role, department: role === 'admin' ? '교통·국토' : null, name: '테스트 계정' } }));
}
function server(rows = [baseRecord], counts = () => jsonResponse({ categories: [{ category: '교통·국토', count: 1 }] })) {
  const calls = [];
  vi.stubGlobal('fetch', vi.fn(async (url, options = {}) => {
    const location = new URL(url); const path = location.pathname;
    calls.push({ path, search: location.search, ...options });
    if (path.endsWith('/counts')) return counts();
    if (path.endsWith('/responses/send')) {
      const content = JSON.parse(options.body).content.trim();
      return jsonResponse({ complaint_status: '완료', response: { content, response_state: 'sent' } });
    }
    if (path.endsWith('/restore')) return jsonResponse({ restored: 1 });
    if (path.endsWith('/permanent')) return jsonResponse({ permanently_deleted: 1 });
    if (path === '/api/complaints') return jsonResponse({ complaints: rows, total: rows.length });
    return jsonResponse({ complaint: rows[0] });
  }));
  return calls;
}

describe('민원 작업 화면 회귀 테스트', () => {
  it('applies and clears department/status lookup on the regular citizen complaint screen', async () => {
    login('user'); const calls = server([{ ...baseRecord, latest_response: null, latest_response_state: null }]); render(<App />);
    fireEvent.click(screen.getByRole('button', { name: '작성한 민원', exact: true }));
    await screen.findByText(baseRecord.title);
    fireEvent.click(screen.getByText('특정 민원 조회'));
    const filters = screen.getByRole('region', { name: '민원 조회 조건' });
    expect(within(filters).getByRole('button', { name: '조회하기' }).disabled).toBe(true);
    fireEvent.click(within(filters).getByRole('checkbox', { name: '교통·국토' }));
    fireEvent.click(within(filters).getByRole('checkbox', { name: '환경·위생' }));
    fireEvent.click(within(filters).getByRole('radio', { name: '접수 중' }));
    fireEvent.click(within(filters).getByRole('button', { name: '조회하기' }));
    await waitFor(() => {
      const filtered = calls.filter((call) => call.path === '/api/complaints').at(-1);
      const params = new URLSearchParams(filtered.search);
      expect(params.getAll('departments')).toEqual(['교통·국토', '환경·위생']);
      expect(params.get('view_status')).toBe('in_progress');
      expect(params.get('offset')).toBe('0');
    });
    fireEvent.click(within(filters).getByRole('button', { name: '조건 초기화' }));
    await waitFor(() => {
      const params = new URLSearchParams(calls.filter((call) => call.path === '/api/complaints').at(-1).search);
      expect(params.has('departments')).toBe(false); expect(params.has('view_status')).toBe(false);
    });
  });

  it('sends the current edited answer and shows the server-confirmed text', async () => {
    login(); const calls = server(); render(<App />);
    fireEvent.click(screen.getByRole('button', { name: '분류 목록', exact: true }));
    const article = (await screen.findByText(baseRecord.title)).closest('article');
    fireEvent.click(within(article).getByRole('button', { name: '민원 접수', exact: true }));
    fireEvent.change(await screen.findByLabelText('답변 내용'), { target: { value: '  수정한 최종 답변입니다.  ' } });
    fireEvent.click(screen.getByRole('button', { name: '답변 전송', exact: true }));
    await screen.findByText('민원 답변이 완료되었습니다.');
    const request = calls.find((call) => call.path.endsWith('/responses/send'));
    expect(JSON.parse(request.body)).toEqual({ content: '  수정한 최종 답변입니다.  ' });
    expect(screen.getByText('수정한 최종 답변입니다.')).toBeTruthy();
    expect(screen.queryByText('처음 저장한 답변')).toBeNull();
    expect(screen.queryByLabelText('답변 내용')).toBeNull();
  });

  it('rejects a blank edited answer without sending the old draft', async () => {
    login(); const calls = server(); render(<App />);
    fireEvent.click(screen.getByRole('button', { name: '분류 목록', exact: true }));
    const article = (await screen.findByText(baseRecord.title)).closest('article');
    fireEvent.click(within(article).getByRole('button', { name: '민원 접수', exact: true }));
    fireEvent.change(await screen.findByLabelText('답변 내용'), { target: { value: '  ' } });
    fireEvent.click(screen.getByRole('button', { name: '답변 전송', exact: true }));
    expect(await screen.findByText('전송할 답변 내용을 입력해 주세요.')).toBeTruthy();
    expect(calls.some((call) => call.path.endsWith('/responses/send'))).toBe(false);
  });

  it('allows restore and individual permanent delete for cancelled own-department trash', async () => {
    login(); const calls = server([{ ...baseRecord, complaint_status: '취소', deleted_at: '2026-10-13', cancelled_by_role: 'user' }]);
    vi.stubGlobal('confirm', vi.fn(() => true));
    vi.stubGlobal('prompt', vi.fn(() => 'test-password'));
    render(<App />);
    fireEvent.click(screen.getByRole('button', { name: '삭제된 데이터', exact: true }));
    const article = (await screen.findByText(baseRecord.title)).closest('article');
    fireEvent.click(within(article).getByRole('button', { name: '복원', exact: true }));
    await screen.findByText('민원을 복원했습니다.');
    expect(calls.some((call) => call.path === '/api/complaints/77/restore')).toBe(true);
    fireEvent.click(within(article).getByRole('button', { name: '영구 삭제', exact: true }));
    await screen.findByText('1건을 영구 삭제했습니다.');
    expect(JSON.parse(calls.find((call) => call.path.endsWith('/permanent')).body)).toEqual({ password: 'test-password' });
  });

  it('keeps other-department trash read-only regardless of cancellation status', async () => {
    login(); server([{ ...baseRecord, category: '환경·위생', can_manage: false, complaint_status: '취소', deleted_at: '2026-10-13' }]); render(<App />);
    fireEvent.click(screen.getByRole('button', { name: '삭제된 데이터', exact: true }));
    const article = (await screen.findByText(baseRecord.title)).closest('article');
    expect(within(article).getByText('읽기 전용')).toBeTruthy();
    expect(within(article).queryByRole('button')).toBeNull();
  });

  it('does not expose archive actions for cancelled active complaints', async () => {
    login(); server([{ ...baseRecord, complaint_status: '취소', deleted_at: null }]); render(<App />);
    fireEvent.click(screen.getByRole('button', { name: '분류 목록', exact: true }));
    const article = (await screen.findByText(baseRecord.title)).closest('article');
    expect(within(article).getByText('취소 · 처리 불가')).toBeTruthy();
    expect(within(article).queryByRole('button')).toBeNull();
  });

  it('keeps a successful list usable when category counts fail', async () => {
    login(); server([baseRecord], () => jsonResponse({ detail: '건수 조회 실패' }, 503)); render(<App />);
    fireEvent.click(screen.getByRole('button', { name: '분류 목록', exact: true }));
    expect(await screen.findByText(baseRecord.title)).toBeTruthy();
    expect(await screen.findByText(/카테고리 건수를 불러오지 못했습니다/)).toBeTruthy();
    expect(screen.getByText('1건')).toBeTruthy();
  });

  it('publishes list rows before an unfinished count request resolves', async () => {
    let resolveCounts;
    const pendingCounts = new Promise((resolve) => { resolveCounts = resolve; });
    login(); server([baseRecord], async () => jsonResponse(await pendingCounts)); render(<App />);
    fireEvent.click(screen.getByRole('button', { name: '분류 목록', exact: true }));
    expect(await screen.findByText(baseRecord.title)).toBeTruthy();
    resolveCounts({ categories: [{ category: '교통·국토', count: 4 }] });
    await waitFor(() => expect(screen.getByRole('button', { name: '4교통·국토' })).toBeTruthy());
  });

  it('refreshes category counts even when the complaint list request fails', async () => {
    login();
    vi.stubGlobal('fetch', vi.fn(async (url) => new URL(url).pathname.endsWith('/counts')
      ? jsonResponse({ categories: [{ category: '교통·국토', count: 6 }] })
      : jsonResponse({ detail: '목록 조회 실패' }, 503)));
    render(<App />); fireEvent.click(screen.getByRole('button', { name: '분류 목록', exact: true }));
    expect(await screen.findByText('목록 조회 실패')).toBeTruthy();
    expect(screen.getByRole('button', { name: '6교통·국토' })).toBeTruthy();
  });
});
