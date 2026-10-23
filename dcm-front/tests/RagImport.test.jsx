import { afterEach, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import App from '../src/App.jsx';

afterEach(() => { cleanup(); sessionStorage.clear(); localStorage.clear(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });
const response = (value) => new Response(JSON.stringify(value), { status: 200 });
const reference = (patch = {}) => ({ id: 'rag-job', source_file: 'rules.csv', status: 'processing', stage: 'embedding', total_rows: 205, completed_rows: 0, saved_rows: 0, duplicate_rows: 0, failed_rows: 0, ...patch });
function setup() {
  const user = { role: 'admin', name: '관리자', department_id: 2, department_name: '교통·국토' };
  sessionStorage.setItem('complaintai.fastapi.auth', JSON.stringify({ token: 'test-only', user }));
  const control = { calls: [], state: reference(), latest: null, failures: [] };
  vi.stubGlobal('fetch', vi.fn(async (url, options = {}) => {
    const path = new URL(url).pathname;
    control.calls.push({ path, ...options });
    if (path === '/api/auth/context') return response({ user });
    if (path === '/api/departments') return response({ departments: [{ id: 2, name: '교통·국토' }, { id: 7, name: '기타' }] });
    if (path === '/api/complaints/counts') return response({ categories: [] });
    if (path === '/api/complaints') return response({ complaints: [], total: 0 });
    if (path === '/api/rag-imports/latest') return response({ job: control.latest });
    if (path === '/api/rag-imports' && options.method === 'POST') {
      if (control.holdUpload) return new Promise((resolve) => { control.finishUpload = () => resolve(response(reference())); });
      return response(reference());
    }
    if (path === '/api/rag-imports/rag-job') {
      if (control.holdPoll) return new Promise((resolve) => { const old = control.state; control.finishPoll = () => resolve(response(old)); });
      return response(control.state);
    }
    if (path === '/api/rag-imports/rag-job/failures') {
      const params = new URL(url).searchParams;
      const offset = Number(params.get('offset') || 0), limit = Number(params.get('limit') || 20);
      return response({ failures: control.failures.slice(offset, offset + limit), total: control.failures.length, offset });
    }
    if (path === '/api/rag-imports/rag-job/cancel') {
      control.state = reference({ status: 'cancelled', stage: 'cancelled', completed_rows: 100, saved_rows: 100 });
      return response({ ...control.state, message: '해당 작업이 취소되었습니다. 이미 저장된 데이터는 유지됩니다.' });
    }
    if (path === '/api/rag-imports/rag-job/resume') {
      control.state = reference({ status:'queued', stage:'chunking', completed_rows:100, saved_rows:100, deferred_rows:1, resumable_rows:1 });
      return response(control.state);
    }
    if (path === '/api/imports') return response({ job_id: 'complaint-job', status: 'queued', total_rows: 300, needs_mapping: false });
    if (path === '/api/imports/complaint-job') return response({ status: 'processing', completed_rows: 100, total_rows: 300 });
    throw new Error(`Unexpected API ${path}`);
  }));
  control.intervals = vi.spyOn(window, 'setInterval');
  return control;
}
function openImports() {
  render(<App />);
  fireEvent.click(screen.getByRole('button', { name: '파일 자료 관리', exact: true }));
}
const panel = () => screen.getByRole('region', { name: 'RAG용 데이터 삽입' });
function chooseFile(name = 'rules.csv') {
  fireEvent.change(within(panel()).getByLabelText(/RAG CSV 파일 선택/), { target: { files: [new File(['제목,본문\n규칙,원문'], name)] } });
}
async function start(control) {
  chooseFile(); fireEvent.click(within(panel()).getByRole('button', { name: 'RAG 데이터 처리 시작' }));
  await screen.findByRole('progressbar', { name: 'RAG 파일 처리' });
  await waitFor(() => expect(control.intervals.mock.calls.some((call) => call[1] === 1000)).toBe(true));
  return control.intervals.mock.calls.filter((call) => call[1] === 1000).at(-1)[0];
}

it('rejects non-CSV files with an explanation before sending an upload', async () => {
  const control = setup(); openImports();
  chooseFile('rules.pdf');
  fireEvent.click(within(panel()).getByRole('button', { name: 'RAG 데이터 처리 시작' }));
  expect(await screen.findByText('파일 형식이 다릅니다. RAG용 데이터는 CSV 파일만 업로드할 수 있습니다.')).toBeTruthy();
  expect(control.calls.filter((call) => call.path === '/api/rag-imports')).toHaveLength(0);
});

it('places independent complaint and RAG progress in the sidebar in that order', async () => {
  const control = setup(); openImports();
  fireEvent.change(screen.getByLabelText(/^민원 파일 선택/), { target: { files: [new File(['제목,본문'], 'complaints.csv')] } });
  fireEvent.click(screen.getByRole('button', { name: '외부 민원 저장 시작' }));
  await screen.findByRole('progressbar', { name: '민원 파일 처리' });
  const poll = await start(control);
  control.state = reference({ completed_rows: 100, saved_rows: 100 });
  await act(async () => { await poll(); });
  const bars = within(screen.getByRole('complementary')).getAllByRole('progressbar');
  expect(bars.map((bar) => bar.getAttribute('aria-label'))).toEqual(['민원 파일 처리', 'RAG 파일 처리']);
  expect(bars[1].getAttribute('aria-valuenow')).toBe('48');
  expect(screen.getByText('100 / 205건 확정 처리')).toBeTruthy();
  const upload = control.calls.find((call) => call.path === '/api/rag-imports');
  expect(upload.body.get('department_id')).toBe('2');
  expect(upload.body.get('file').name).toBe('rules.csv');
  control.state = reference({ status: 'completed', stage: 'completed', completed_rows: 205, saved_rows: 200, duplicate_rows: 5 });
  await act(async () => { await poll(); });
  expect(screen.getByRole('progressbar', { name: 'RAG 파일 처리' }).getAttribute('aria-valuenow')).toBe('100');
  expect(await screen.findByText(/RAG 자료 처리가 완료되었습니다. 저장 200건 · 중복 5건/)).toBeTruthy();
  expect(within(panel()).queryByText(/처리하지 못한 항목|읽을 수 없는 항목/)).toBeNull();
  expect(control.calls.some((call) => call.path.endsWith('/failures'))).toBe(false);
  expect(within(panel()).getByRole('button', { name: 'RAG 데이터 처리 시작' }).disabled).toBe(false);
});

it('cancels from another screen and prevents a stale poll from reviving progress', async () => {
  const control = setup(); openImports();
  const poll = await start(control);
  control.state = reference({ completed_rows: 100, saved_rows: 100 });
  control.holdPoll = true;
  let pending;
  await act(async () => { pending = poll(); });
  fireEvent.click(screen.getByRole('button', { name: '분류 목록 · 민원 접수', exact: true }));
  fireEvent.click(screen.getByRole('button', { name: 'RAG 작업 취소' }));
  await screen.findByText('해당 작업이 취소되었습니다.');
  await act(async () => { control.finishPoll(); await pending; });
  expect(screen.getByText('작업 취소됨')).toBeTruthy();
  expect(screen.getByText('저장 100 · 중복 0')).toBeTruthy();
  expect(screen.queryByRole('button', { name: 'RAG 작업 취소' })).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: '파일 자료 관리', exact: true }));
  expect(screen.getByText('해당 작업이 취소되었습니다. 이미 저장된 데이터는 유지됩니다.')).toBeTruthy();
});

it('honors cancellation requested while the upload response is pending', async () => {
  const control = setup(); control.holdUpload = true; openImports();
  chooseFile(); fireEvent.click(within(panel()).getByRole('button', { name: 'RAG 데이터 처리 시작' }));
  fireEvent.click(within(panel()).getByRole('button', { name: 'RAG 작업 취소' }));
  expect(within(panel()).getByRole('button', { name: '취소 중...' }).disabled).toBe(true);
  await act(async () => control.finishUpload());
  await screen.findByText('해당 작업이 취소되었습니다. 이미 저장된 데이터는 유지됩니다.');
  expect(control.calls.filter((call) => call.path.endsWith('/cancel'))).toHaveLength(1);
});

it('restores the latest job after reload without starting another upload', async () => {
  const control = setup(); control.latest = reference({ completed_rows: 100, saved_rows: 99, duplicate_rows: 1 }); openImports();
  await screen.findByText('100 / 205건 확정 처리');
  expect(within(panel()).getByRole('button', { name: 'RAG 자료 처리 중...' }).disabled).toBe(true);
  expect(control.calls.some((call) => call.method === 'POST')).toBe(false);
});

it('completes despite excluded rows and shows their row numbers and reasons separately', async () => {
  const control = setup(); openImports();
  const poll = await start(control);
  control.state = reference({ status: 'completed', stage: 'completed', completed_rows: 205, saved_rows: 203, failed_rows: 2 });
  control.failures = [{ source_row: 19, reason: '임베딩 입력이 8,192토큰을 초과했습니다. (HTTP 413)' }, { source_row: 102, reason: '임베딩 서버 응답 오류' }];
  await act(async () => { await poll(); });
  expect(await screen.findByText(/RAG 자료 처리가 완료되었습니다.*처리하지 못한 항목 2건 제외/)).toBeTruthy();
  expect(screen.getByRole('progressbar', { name: 'RAG 파일 처리' }).getAttribute('aria-valuenow')).toBe('100');
  fireEvent.click(screen.getByText('처리하지 못한 항목 2건'));
  expect(await screen.findByText('CSV 19행')).toBeTruthy();
  expect(screen.getByText(/HTTP 413/)).toBeTruthy();
  expect(screen.getByText('CSV 102행')).toBeTruthy();
  expect(within(panel()).getByRole('button', { name: 'RAG 데이터 처리 시작' }).disabled).toBe(false);
});

it('restores exception counts after reload and paginates the details', async () => {
  const control = setup();
  control.latest = reference({ status: 'completed', stage: 'completed', completed_rows: 205, saved_rows: 182, failed_rows: 23 });
  control.failures = Array.from({ length: 23 }, (_, i) => ({ source_row: i + 2, reason: `저장하지 못한 항목 ${i + 1}` }));
  openImports();
  expect(await screen.findByText(/RAG 자료 처리가 완료되었습니다.*처리하지 못한 항목 23건 제외/)).toBeTruthy();
  fireEvent.click(screen.getByText('처리하지 못한 항목 23건'));
  expect(await screen.findByText('CSV 21행')).toBeTruthy();
  expect(screen.queryByText('CSV 24행')).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: '다음 20건 보기' }));
  expect(await screen.findByText('CSV 24행')).toBeTruthy();
  expect(screen.queryByRole('button', { name: '다음 20건 보기' })).toBeNull();
});

it('shows deferred originals separately from committed chunk progress', async () => {
  const control=setup();openImports();
  const poll=await start(control);
  control.state=reference({ stage:'chunking',completed_rows:204,saved_rows:204,deferred_rows:1,long_processed_rows:0,
    active_source_row:19,active_chunks_total:13,active_chunks_completed:8 });
  await act(async()=>{await poll();});
  expect(screen.getByText('긴 원문 조각 처리 중')).toBeTruthy();
  expect(screen.getByText('긴 원문 후순위 1건 · 미처리 1건')).toBeTruthy();
  expect(screen.getByText('CSV 19행 · 조각 8 / 13개 저장')).toBeTruthy();
  expect(screen.getByRole('progressbar',{name:'RAG 파일 처리'}).getAttribute('aria-valuenow')).toBe('99');
  expect(within(panel()).queryByText(/처리하지 못한 항목/)).toBeNull();
});

it('resumes a preserved long original without uploading its CSV again', async () => {
  const control=setup();
  control.latest=reference({ status:'cancelled',stage:'cancelled',completed_rows:100,saved_rows:100,deferred_rows:1,resumable_rows:1 });
  openImports();
  const button=await within(panel()).findByRole('button',{name:'긴 원문 이어서 처리'});
  fireEvent.click(button);fireEvent.click(button);
  await screen.findByText('긴 원문 후순위 대기');
  expect(control.calls.filter(call=>call.path.endsWith('/resume'))).toHaveLength(1);
  expect(control.calls.some(call=>call.path==='/api/rag-imports' && call.method==='POST')).toBe(false);
  expect(within(panel()).getByRole('button',{name:'RAG 자료 처리 중...'}).disabled).toBe(true);
});
