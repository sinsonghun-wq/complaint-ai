import { StrictMode } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import CitizenComplaintTools from '../src/CitizenComplaintTools.jsx';

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
