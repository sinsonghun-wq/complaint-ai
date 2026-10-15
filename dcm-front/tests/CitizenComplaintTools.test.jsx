import { StrictMode } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import CitizenComplaintTools from '../src/CitizenComplaintTools.jsx';
import ComplaintFilters, { appendComplaintFilters, citizenViewStatusLabel, departmentLabels } from '../src/ComplaintFilters.jsx';

afterEach(() => { cleanup(); vi.useRealTimers(); });
const row = (overrides = {}) => ({ id: 42, title: '도로 보수 요청', content: '보도블록이 깨져 통행이 어렵습니다.', complaint_status: '접수', category: '국토교통', latest_response: '', latest_response_state: '', ...overrides });
const request = (intent, extra = {}) => ({ key: `${intent}-request`, intent, ...extra });
const mutations = (api) => api.mock.calls.filter(([, options]) => options?.method);
const defaults = (api, extras = {}) => ({ api, onContextChange: vi.fn(), onChanged: vi.fn(), onRecommend: vi.fn(), onClose: vi.fn(), ...extras });
const ownApi = (record = row()) => vi.fn(async (path, options) => {
  if (options?.method === 'DELETE') return { message: '민원이 취소되었습니다.' };
  if (options?.method) return { complaint: { ...record, ...JSON.parse(options.body) } };
  return path.includes('?') ? { complaints: [record], total: 1 } : { complaint: record };
});

describe('complaint lookup filters and cached chatbot search', () => {
  it('requires a department and exactly one status before applying the shared filter', () => {
    const apply = vi.fn(); const clear = vi.fn();
    render(<ComplaintFilters onApply={apply} onClear={clear} />);
    expect(screen.getAllByRole('checkbox')).toHaveLength(7);
    const button = screen.getByRole('button', { name: '조회하기' });
    expect(button.disabled).toBe(true);
    fireEvent.click(screen.getByRole('checkbox', { name: departmentLabels[1] }));
    expect(button.disabled).toBe(true);
    fireEvent.click(screen.getByRole('radio', { name: '접수 중' }));
    expect(button.disabled).toBe(false);
    fireEvent.click(screen.getByRole('checkbox', { name: departmentLabels[3] }));
    fireEvent.click(button);
    expect(apply).toHaveBeenCalledWith({ departments: [departmentLabels[1], departmentLabels[3]], view_status: 'in_progress' });
    fireEvent.click(screen.getByRole('radio', { name: '미접수' }));
    expect(screen.getByRole('radio', { name: '접수 중' }).checked).toBe(false);
    expect(screen.getByText(/선택한 부서와 관계없이 본인의 부서 미정/)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: '조건 초기화' }));
    expect(button.disabled).toBe(true); expect(clear).toHaveBeenCalledOnce();
  });

  it('serializes repeated department filters without accepting unknown names or duplicating earlier values', () => {
    const params = appendComplaintFilters(new URLSearchParams('limit=10&departments=old&view_status=old'), { departments: [departmentLabels[1], departmentLabels[1], '없는 부서', departmentLabels[3]], view_status: 'completed' });
    expect(params.getAll('departments')).toEqual([departmentLabels[1], departmentLabels[3]]);
    expect(params.get('view_status')).toBe('completed'); expect(params.get('limit')).toBe('10');
    appendComplaintFilters(params, null);
    expect(params.has('departments')).toBe(false); expect(params.has('view_status')).toBe(false);
  });

  it('uses the same department-and-official-response statuses as lookup filters', () => {
    expect(citizenViewStatusLabel(row({ category: null }))).toBe('미접수');
    expect(citizenViewStatusLabel(row({ latest_response_state: 'draft' }))).toBe('접수 중');
    expect(citizenViewStatusLabel(row({ complaint_status: '접수', latest_response_state: 'sent' }))).toBe('접수 완료');
    expect(citizenViewStatusLabel(row({ complaint_status: '취소', latest_response_state: 'sent' }))).toBe('취소');
  });

  it('applies server-side filters, preserves them after detail and can clear them', async () => {
    const api = ownApi();
    render(<CitizenComplaintTools {...defaults(api)} request={request('list')} />);
    fireEvent.click(await screen.findByRole('button', { name: '조회 옵션' }));
    const filters = screen.getByRole('region', { name: '민원 조회 조건' });
    fireEvent.click(within(filters).getByRole('checkbox', { name: departmentLabels[1] }));
    fireEvent.click(within(filters).getByRole('checkbox', { name: departmentLabels[3] }));
    fireEvent.click(within(filters).getByRole('radio', { name: '접수 중' }));
    fireEvent.click(within(filters).getByRole('button', { name: '조회하기' }));
    await waitFor(() => expect(api.mock.calls.some(([path]) => new URL(path, 'http://local').searchParams.get('view_status') === 'in_progress')).toBe(true));
    const filteredPath = api.mock.calls.find(([path]) => path.includes('view_status='))[0];
    expect(new URL(filteredPath, 'http://local').searchParams.getAll('departments')).toEqual([departmentLabels[1], departmentLabels[3]]);
    fireEvent.click(await screen.findByRole('button', { name: '민원 #42 도로 보수 요청 확인' }));
    fireEvent.click(await screen.findByRole('button', { name: '목록으로 돌아가기' }));
    await screen.findByRole('button', { name: '민원 #42 도로 보수 요청 확인' });
    fireEvent.click(screen.getByRole('button', { name: '조회 옵션' }));
    expect(screen.getByRole('checkbox', { name: departmentLabels[1] }).checked).toBe(true);
    expect(screen.getByRole('radio', { name: '접수 중' }).checked).toBe(true);
    fireEvent.click(screen.getByRole('button', { name: '조건 초기화' }));
    await waitFor(() => expect(api.mock.calls.at(-1)[0]).toBe('/api/complaints?deleted=false&limit=10&offset=0'));
    expect(screen.queryByText(/^조회 조건:/)).toBeNull();
  });

  it('honors an external filter request before loading a generic list', async () => {
    const api = ownApi();
    render(<CitizenComplaintTools {...defaults(api)} request={request('filter', { filters: { departments: [departmentLabels[5]], view_status: 'unassigned' } })} />);
    await screen.findByRole('button', { name: '민원 #42 도로 보수 요청 확인' });
    expect(api.mock.calls.filter(([path]) => path.includes('?')).every(([path]) => new URL(path, 'http://local').searchParams.get('view_status') === 'unassigned')).toBe(true);
    expect(screen.getByText(/^조회 조건:/).textContent).toContain('미접수');
  });

  it('updates the same search request from loading to complete, opens details and returns to both result groups', async () => {
    const direct = row(); const similar = row({ id: 43, title: '인도 파손 신고' });
    const api = vi.fn(async (path) => ({ complaint: path.endsWith('/43') ? similar : direct }));
    const props = defaults(api);
    const searchRequest = request('search', { query: '보도블록 관련 민원', state: 'loading' });
    const { rerender } = render(<CitizenComplaintTools {...props} request={searchRequest} />);
    expect(screen.getByText('관련 민원을 검색하고 있습니다.')).toBeTruthy();
    expect(screen.getByRole('button', { name: '새 민원 작성' }).disabled).toBe(true);
    expect(screen.getByRole('button', { name: '작성한 민원 확인' }).disabled).toBe(true);
    expect(api).not.toHaveBeenCalled();
    rerender(<CitizenComplaintTools {...props} request={{ ...searchRequest, state: 'complete', results: { complaints: [direct], similar_complaints: [similar], total: 1, similar_total: 1 } }} />);
    const directList = await screen.findByRole('region', { name: '검색된 민원' });
    expect(within(directList).getByRole('button', { name: '민원 #42 도로 보수 요청 확인' })).toBeTruthy();
    expect(within(screen.getByRole('region', { name: '검색과 유사한 민원' })).getByRole('button', { name: '민원 #43 인도 파손 신고 확인' })).toBeTruthy();
    fireEvent.click(within(directList).getByRole('button', { name: '민원 #42 도로 보수 요청 확인' }));
    await screen.findByRole('button', { name: '민원 수정' });
    fireEvent.click(screen.getByRole('button', { name: '목록으로 돌아가기' }));
    await screen.findByRole('region', { name: '검색과 유사한 민원' });
    expect(screen.getByText('검색 내용: 보도블록 관련 민원')).toBeTruthy();
    expect(mutations(api)).toHaveLength(0);
    expect(api.mock.calls.every(([path]) => /^\/api\/complaints\/\d+$/.test(path))).toBe(true);
  });

  it('clears earlier rows when a later search fails and shows the failure without fallback results', async () => {
    const api = ownApi();
    const props = defaults(api);
    const { rerender } = render(<CitizenComplaintTools {...props} request={request('search', { state: 'complete', query: '첫 검색', results: { complaints: [row()], similar_complaints: [] } })} />);
    await screen.findByRole('button', { name: '민원 #42 도로 보수 요청 확인' });
    rerender(<CitizenComplaintTools {...props} request={{ key: 'next-search', intent: 'search', state: 'failed', query: '다른 검색', error: '검색 모델 응답 실패' }} />);
    await screen.findByRole('alert');
    expect(screen.getByRole('alert').textContent).toBe('검색 모델 응답 실패');
    expect(screen.queryByRole('button', { name: '민원 #42 도로 보수 요청 확인' })).toBeNull();
    expect(screen.getByText('검색 내용: 다른 검색')).toBeTruthy();
  });

  it('paginates direct and similar results independently and reads only visible result IDs', async () => {
    const direct = Array.from({ length: 12 }, (_, index) => row({ id: index + 1 }));
    const similar = Array.from({ length: 12 }, (_, index) => row({ id: index + 101 }));
    const all = [...direct, ...similar];
    const api = vi.fn(async (path) => ({ complaint: all.find(({ id }) => id === Number(path.split('/').at(-1))) }));
    render(<CitizenComplaintTools {...defaults(api)} request={request('search', { state: 'complete', query: '도로 관련', results: { complaints: direct, similar_complaints: similar } })} />);
    await screen.findByRole('button', { name: '민원 #1 도로 보수 요청 확인' });
    await waitFor(() => expect(api.mock.calls).toHaveLength(20));
    expect(api.mock.calls.some(([path]) => path === '/api/complaints/11' || path === '/api/complaints/111')).toBe(false);
    fireEvent.click(screen.getByRole('button', { name: '검색된 민원 다음' }));
    await screen.findByRole('button', { name: '민원 #11 도로 보수 요청 확인' });
    await waitFor(() => expect(api).toHaveBeenCalledWith('/api/complaints/11'));
    expect(screen.getByRole('button', { name: '민원 #101 도로 보수 요청 확인' })).toBeTruthy();
    expect(screen.queryByRole('button', { name: '민원 #111 도로 보수 요청 확인' })).toBeNull();
  });

  it('removes an edited complaint from cached semantic results and asks for a new search', async () => {
    let current = row();
    const api = vi.fn(async (path, options) => {
      if (options?.method === 'PATCH') { current = { ...current, ...JSON.parse(options.body) }; return { complaint: current }; }
      return { complaint: current };
    });
    const props = defaults(api);
    render(<CitizenComplaintTools {...props} request={request('search', { state: 'complete', query: '도로', results: { complaints: [row()], similar_complaints: [] } })} />);
    fireEvent.click(await screen.findByRole('button', { name: '민원 #42 도로 보수 요청 확인' }));
    fireEvent.click(await screen.findByRole('button', { name: '민원 수정' }));
    fireEvent.change(screen.getByLabelText('도구 민원 내용'), { target: { value: '수정된 내용' } });
    fireEvent.click(screen.getByRole('button', { name: '수정 저장' }));
    await screen.findByText('민원이 수정되었습니다. 수정한 민원은 이전 검색 결과에서 제외했습니다. 다시 검색해 주세요.');
    fireEvent.click(screen.getByRole('button', { name: '목록으로 돌아가기' }));
    await screen.findByText('검색 내용에 해당하는 민원이 없습니다.');
    expect(screen.queryByRole('button', { name: '민원 #42 도로 보수 요청 확인' })).toBeNull();
    expect(mutations(api)).toHaveLength(1);
  });

  it('removes externally changed or deleted cached results on a status refresh without rerunning search', async () => {
    const direct = row(); const similar = row({ id: 43, title: '유사 민원' });
    const api = vi.fn(async (path) => ({ complaint: path.endsWith('/43') ? { ...similar, complaint_status: '취소', deleted_at: '2026-10-14' } : { ...direct, content: '검색 이후 변경된 원문' } }));
    render(<CitizenComplaintTools {...defaults(api)} request={request('search', { state: 'complete', query: '도로', results: { complaints: [direct], similar_complaints: [similar] } })} />);
    await screen.findByText('내용이 변경된 민원을 이전 검색 결과에서 제외했습니다. 다시 검색해 주세요.');
    expect(screen.queryByRole('button', { name: '민원 #42 도로 보수 요청 확인' })).toBeNull();
    expect(screen.queryByRole('button', { name: '민원 #43 유사 민원 확인' })).toBeNull();
    expect(screen.getByText('검색 내용에 해당하는 민원이 없습니다.')).toBeTruthy();
    expect(screen.getByText('추가로 표시할 유사 민원이 없습니다.')).toBeTruthy();
    expect(mutations(api)).toHaveLength(0);
  });
});

describe('citizen complaint side tools', () => {
  it('posts the same citizen endpoint only after a submit click and shows the saved detail', async () => {
    const api = ownApi();
    const props = defaults(api, { request: request('create') });
    render(<CitizenComplaintTools {...props} />);
    fireEvent.change(screen.getByLabelText('도구 민원 제목'), { target: { value: '새 제목' } });
    fireEvent.change(screen.getByLabelText('도구 민원 내용'), { target: { value: '새 내용' } });
    expect(mutations(api)).toHaveLength(0);
    fireEvent.click(screen.getByRole('button', { name: '민원 접수', exact: true }));
    await screen.findByText('민원이 접수되었습니다.');
    const [[path, options]] = mutations(api);
    expect(path).toBe('/api/complaints');
    expect(options.method).toBe('POST');
    expect(JSON.parse(options.body)).toEqual({ title: '새 제목', content: '새 내용' });
    expect(props.onChanged).toHaveBeenCalledWith(expect.objectContaining({ action: 'create', record: expect.objectContaining({ id: 42 }) }));
    expect(screen.getByRole('button', { name: '이 민원으로 AI 답변 보기' })).toBeTruthy();
  });

  it('retains an unsaved draft and selection across hiding and reopening without rerunning the request', async () => {
    const props = defaults(ownApi(), { request: request('create') });
    const { rerender } = render(<CitizenComplaintTools {...props} />);
    fireEvent.change(screen.getByLabelText('도구 민원 제목'), { target: { value: '작성 중인 제목' } });
    fireEvent.change(screen.getByLabelText('도구 민원 내용'), { target: { value: '작성 중인 내용' } });
    fireEvent.click(screen.getByRole('button', { name: '도구 창 닫기' }));
    expect(props.onClose).toHaveBeenCalledOnce();
    rerender(<CitizenComplaintTools {...props} visible={false} />);
    expect(screen.queryByRole('region', { name: '민원 도구' })).toBeNull();
    rerender(<CitizenComplaintTools {...props} visible />);
    expect(screen.getByLabelText('도구 민원 제목').value).toBe('작성 중인 제목');
    expect(screen.getByLabelText('도구 민원 내용').value).toBe('작성 중인 내용');
    expect(props.onContextChange).toHaveBeenLastCalledWith({ mode: 'create', complaint_id: null, title: '작성 중인 제목', content: '작성 중인 내용' });
    expect(mutations(props.api)).toHaveLength(0);
  });

  it('passes an unsaved draft to RAG without creating a real complaint', () => {
    const api = ownApi();
    const props = defaults(api, { request: request('create', { draft: { title: '작성 초안', content: '초안 내용' } }) });
    render(<CitizenComplaintTools {...props} />);
    fireEvent.click(screen.getByRole('button', { name: '작성 내용으로 AI 답변 보기' }));
    expect(props.onRecommend).toHaveBeenCalledWith({ title: '작성 초안', content: '초안 내용' });
    expect(mutations(api)).toHaveLength(0);
  });

  it('opens the requested owned record and sends PATCH only after save', async () => {
    const api = ownApi();
    const props = defaults(api, { request: request('edit', { complaint_id: 42 }) });
    render(<CitizenComplaintTools {...props} />);
    await screen.findByLabelText('도구 민원 내용');
    expect(api).toHaveBeenCalledWith('/api/complaints/42');
    expect(mutations(api)).toHaveLength(0);
    fireEvent.change(screen.getByLabelText('도구 민원 내용'), { target: { value: '수정된 원문' } });
    fireEvent.click(screen.getByRole('button', { name: '수정 저장' }));
    await screen.findByText('민원이 수정되었습니다.');
    const [[path, options]] = mutations(api);
    expect(path).toBe('/api/complaints/42'); expect(options.method).toBe('PATCH');
    expect(JSON.parse(options.body)).toEqual({ title: row().title, content: '수정된 원문' });
    expect(props.onChanged).toHaveBeenCalledWith(expect.objectContaining({ action: 'edit' }));
  });

  it('opens a deletion confirmation for an explicit request and supports cancelling before DELETE', async () => {
    const api = ownApi(row({ complaint_status: '진행중' }));
    const props = defaults(api, { request: request('delete', { complaint_id: 42 }) });
    render(<CitizenComplaintTools {...props} />);
    const dialog = await screen.findByRole('dialog', { name: '민원 삭제 확인' });
    expect(mutations(api)).toHaveLength(0);
    fireEvent.click(within(dialog).getByRole('button', { name: '삭제 취소' }));
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(mutations(api)).toHaveLength(0);
    fireEvent.click(screen.getByRole('button', { name: '민원 삭제' }));
    fireEvent.click(screen.getByRole('button', { name: '삭제 확인' }));
    await screen.findByText('민원이 취소되었습니다.');
    const [[path, options]] = mutations(api);
    expect(path).toBe('/api/complaints/42'); expect(options.method).toBe('DELETE');
    expect(props.onChanged).toHaveBeenCalledWith(expect.objectContaining({ action: 'delete' }));
  });

  it.each(['완료', '취소'])('blocks editing and deletion when the complaint is %s', async (status) => {
    const api = ownApi(row({ complaint_status: status, latest_response: status === '완료' ? '전달된 공무원 답변' : '', latest_response_state: status === '완료' ? 'sent' : '' }));
    const props = defaults(api, { request: request('edit', { complaint_id: 42 }) });
    const { rerender } = render(<CitizenComplaintTools {...props} />);
    await screen.findByText('접수 대기 상태의 민원만 수정할 수 있습니다.');
    expect(screen.queryByRole('button', { name: '민원 수정' })).toBeNull();
    expect(screen.queryByRole('button', { name: '민원 삭제' })).toBeNull();
    if (status === '완료') expect(screen.getByText('전달된 공무원 답변')).toBeTruthy();
    rerender(<CitizenComplaintTools {...props} request={request('delete', { complaint_id: 42 })} />);
    await screen.findByText('접수 대기 또는 접수 됨 상태의 민원만 삭제할 수 있습니다.');
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(mutations(api)).toHaveLength(0);
  });

  it('does not display a response marked draft to the citizen', async () => {
    const api = ownApi(row({ latest_response: '아직 전송하지 않은 답변', latest_response_state: 'draft' }));
    render(<CitizenComplaintTools {...defaults(api)} request={request('list', { complaint_id: 42 })} />);
    await screen.findByRole('button', { name: '민원 수정' });
    expect(screen.queryByText('아직 전송하지 않은 답변')).toBeNull();
  });

  it('keeps the edited draft and shows a 409 status race without reporting success', async () => {
    let started = false;
    const api = vi.fn(async (path, options) => {
      if (options?.method === 'PATCH') { started = true; throw new Error('접수 대기 상태의 민원만 수정할 수 있습니다.'); }
      return path.includes('?') ? { complaints: [], total: 0 } : { complaint: row({ complaint_status: started ? '진행중' : '접수' }) };
    });
    const props = defaults(api, { request: request('edit', { complaint_id: 42 }) });
    render(<CitizenComplaintTools {...props} />);
    const content = await screen.findByLabelText('도구 민원 내용');
    fireEvent.change(content, { target: { value: '보관할 수정 초안' } });
    fireEvent.click(screen.getByRole('button', { name: '수정 저장' }));
    await screen.findByRole('alert');
    await waitFor(() => expect(screen.getByRole('button', { name: '수정 저장' }).disabled).toBe(true));
    expect(content.value).toBe('보관할 수정 초안');
    expect(props.onChanged).not.toHaveBeenCalled();
    expect(screen.queryByText('민원이 수정되었습니다.')).toBeNull();
  });

  it('prevents duplicate create clicks and does not treat a later detail-read failure as failed submission', async () => {
    let resolvePost;
    const api = vi.fn((path, options) => {
      if (options?.method === 'POST') return new Promise((resolve) => { resolvePost = resolve; });
      return path.includes('?') ? Promise.resolve({ complaints: [], total: 0 }) : Promise.reject(new Error('상태 조회 실패'));
    });
    const props = defaults(api, { request: request('create', { draft: { title: '제목', content: '내용' } }) });
    render(<CitizenComplaintTools {...props} />);
    const button = screen.getByRole('button', { name: '민원 접수', exact: true });
    fireEvent.click(button); fireEvent.click(button);
    expect(mutations(api)).toHaveLength(1);
    await act(async () => resolvePost({ complaint: row({ title: '제목', content: '내용' }) }));
    await screen.findByText('민원은 저장되었지만 최신 상태를 불러오지 못했습니다. 상태 조회 실패');
    expect(props.onChanged).toHaveBeenCalledOnce();
    expect(screen.getByText('민원이 접수되었습니다.')).toBeTruthy();
    expect(screen.queryByRole('button', { name: '민원 접수', exact: true })).toBeNull();
  });

  it('reuses the completed complaint follow-up endpoint after explicit submission', async () => {
    const completed = row({ complaint_status: '완료', latest_response_state: 'sent', latest_response: '이전 공식 답변' });
    const api = vi.fn(async (path, options) => {
      if (options?.method === 'POST') return { complaint: { id: 43, analysis_revision: 1 }, message: '재민원이 접수되었습니다.' };
      if (path === '/api/complaints/43') return { complaint: row({ id: 43, title: '재민원: 도로 보수 요청', content: '추가 요청', previous_context: { title: completed.title, content: completed.content, response: completed.latest_response } }) };
      return path.includes('?') ? { complaints: [completed], total: 1 } : { complaint: completed };
    });
    const props = defaults(api, { request: request('list', { complaint_id: 42 }) });
    render(<CitizenComplaintTools {...props} />);
    fireEvent.click(await screen.findByRole('button', { name: '재민원 작성', exact: true }));
    fireEvent.change(screen.getByLabelText('도구 민원 내용'), { target: { value: '추가 요청' } });
    expect(mutations(api)).toHaveLength(0);
    fireEvent.click(screen.getByRole('button', { name: '재민원 접수' }));
    await screen.findByText('재민원이 접수되었습니다.');
    const [[path, options]] = mutations(api);
    expect(path).toBe('/api/complaints/42/follow-up'); expect(options.method).toBe('POST');
    expect(props.onChanged).toHaveBeenCalledWith(expect.objectContaining({ action: 'follow-up', record: expect.objectContaining({ id: 43, content: '추가 요청' }) }));
  });

  it('reads only the owned list and pages it', async () => {
    const api = vi.fn(async (path) => ({ complaints: [row({ id: path.includes('offset=10') ? 52 : 42 })], total: 11 }));
    render(<CitizenComplaintTools {...defaults(api)} request={request('list')} />);
    await screen.findByRole('button', { name: '민원 #42 도로 보수 요청 확인' });
    expect(api).toHaveBeenCalledWith('/api/complaints?deleted=false&limit=10&offset=0');
    fireEvent.click(screen.getByRole('button', { name: '다음' }));
    await screen.findByRole('button', { name: '민원 #52 도로 보수 요청 확인' });
    expect(api).toHaveBeenCalledWith('/api/complaints?deleted=false&limit=10&offset=10');
  });

  it('polls status while visible, blocks inaccessible snapshots and resumes only after a successful read', async () => {
    vi.useFakeTimers();
    let inaccessible = false;
    const api = vi.fn(async (path) => {
      if (path.includes('?')) return { complaints: [row()], total: 1 };
      if (inaccessible) throw new Error('민원을 찾을 수 없습니다.');
      return { complaint: row() };
    });
    const props = defaults(api, { request: request('edit', { complaint_id: 42 }) });
    const { rerender } = render(<CitizenComplaintTools {...props} />);
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    const input = screen.getByLabelText('도구 민원 내용');
    fireEvent.change(input, { target: { value: '보관할 초안' } });
    inaccessible = true;
    await act(async () => { await vi.advanceTimersByTimeAsync(3000); });
    expect(screen.getByRole('alert').textContent).toBe('민원을 찾을 수 없습니다.');
    expect(screen.getByRole('button', { name: '수정 저장' }).disabled).toBe(true);
    expect(screen.getByRole('button', { name: '작성 내용으로 AI 답변 보기' }).disabled).toBe(true);
    expect(input.value).toBe('보관할 초안');
    expect(props.onContextChange).toHaveBeenLastCalledWith({ mode: 'edit', complaint_id: null, title: '', content: '' });
    rerender(<CitizenComplaintTools {...props} visible={false} />);
    const readCount = api.mock.calls.length;
    await act(async () => { await vi.advanceTimersByTimeAsync(6000); });
    expect(api.mock.calls).toHaveLength(readCount);
    inaccessible = false;
    rerender(<CitizenComplaintTools {...props} visible />);
    await act(async () => { await vi.advanceTimersByTimeAsync(3000); });
    expect(screen.getByRole('button', { name: '수정 저장' }).disabled).toBe(false);
    expect(input.value).toBe('보관할 초안');
  });

  it('handles a requested target under React StrictMode without losing its detail', async () => {
    const api = ownApi();
    render(<StrictMode><CitizenComplaintTools {...defaults(api)} request={request('edit', { complaint_id: 42 })} /></StrictMode>);
    await screen.findByRole('button', { name: '수정 저장' });
    expect(screen.getByLabelText('도구 민원 내용').value).toBe(row().content);
    expect(mutations(api)).toHaveLength(0);
  });
});
