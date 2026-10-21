import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import AIRecommendations, { useRecommendations } from '../src/AIRecommendations.jsx';
import App from '../src/App.jsx';

afterEach(() => { cleanup(); sessionStorage.clear(); localStorage.clear(); vi.unstubAllGlobals(); });

function Harness({ api, choose = vi.fn() }) {
  const recommendation = useRecommendations(api);
  return <AIRecommendations recommendation={recommendation} onChooseExisting={choose} />;
}

const ownComplaint = {
  id: 42, title: '보행로 보수 요청', content: '학교 앞 보행로가 파손되어 보수가 필요합니다.',
  complaint_status: '접수', analysis_state: 'completed', category: '교통·국토',
  submitted_by_user: true, created_at: '2026-10-13T00:00:00Z', latest_response: '', deleted_at: null,
};
const route = (intent, extra = {}) => ({
  intent, message: intent === 'answer' ? '' : '민원 도구에서 내용을 확인해 주세요.',
  complaint_id: null, draft: null, ...extra,
});
const completed = (id = 'answer-job', answer = '공식 자료를 확인한 추천 답변입니다. [1]') => ({
  id, status: 'completed', title: '질문', content: '질문 내용',
  result: { answer_status: 'answered', answer, checks: [], next_steps: [], citations: [] },
});
const mutations = (api) => api.mock.calls.filter(([path, options]) => path.startsWith('/api/complaints') && ['POST', 'PATCH', 'DELETE'].includes(options?.method));
const ragRequests = (api) => api.mock.calls.filter(([path, options]) => path === '/api/ai-recommendations' && options?.method === 'POST');
const panel = () => screen.getByRole('region', { name: '민원 도구' });
const send = (content) => {
  fireEvent.change(screen.getByLabelText('메시지'), { target: { value: content } });
  fireEvent.click(screen.getByRole('button', { name: '전송', exact: true }));
};

function toolServer(intent = route('answer'), { rows = [ownComplaint], patchFailure = null } = {}) {
  let records = rows.map((row) => ({ ...row }));
  return vi.fn(async (path, options = {}) => {
    const body = options.body ? JSON.parse(options.body) : null;
    if (path === '/api/ai-recommendations/intent') return typeof intent === 'function' ? intent(body) : intent;
    if (path === '/api/ai-recommendations') return completed();
    if (path.startsWith('/api/complaints?')) return { complaints: records, total: records.length };
    if (path === '/api/complaints' && options.method === 'POST') {
      const complaint = { ...ownComplaint, id: 43, ...body, analysis_state: 'pending' };
      records = [complaint, ...records];
      return { complaint, message: '민원이 접수되었습니다.' };
    }
    const match = path.match(/^\/api\/complaints\/(\d+)$/);
    if (match) {
      const id = Number(match[1]);
      const complaint = records.find((row) => row.id === id);
      if (!complaint) throw new Error('본인 민원을 찾을 수 없습니다.');
      if (options.method === 'PATCH') {
        if (patchFailure) throw new Error(patchFailure);
        Object.assign(complaint, body);
        return { complaint: { ...complaint } };
      }
      if (options.method === 'DELETE') {
        records = records.filter((row) => row.id !== id);
        return { deleted: 1, complaint_status: '취소', message: '민원이 취소되었습니다.' };
      }
      return { complaint: { ...complaint } };
    }
    throw new Error(`예상하지 않은 API: ${path}`);
  });
}

describe('대화와 민원 도구 연결', () => {
  it('opens a real create tool from conversation, waits for explicit submission, and keeps the chat visible', async () => {
    const api = toolServer(route('create', { draft: { title: '가로등 수리', content: '집 앞 가로등이 꺼져 있습니다.' } }));
    const choose = vi.fn();
    render(<Harness api={api} choose={choose} />);
    expect(screen.queryByRole('region', { name: '민원 도구' })).toBeNull();
    send('내가 민원을 작성하고 싶어. 집 앞 가로등이 꺼졌어.');
    await screen.findByRole('region', { name: '민원 도구' });
    expect(screen.getByRole('log', { name: '민원 상담 대화' })).toBeTruthy();
    expect((await within(panel()).findByLabelText('도구 민원 제목')).value).toBe('가로등 수리');
    expect(within(panel()).getByLabelText('도구 민원 내용').value).toBe('집 앞 가로등이 꺼져 있습니다.');
    expect(mutations(api)).toHaveLength(0);
    expect(ragRequests(api)).toHaveLength(0);
    expect(choose).not.toHaveBeenCalled();
    fireEvent.change(within(panel()).getByLabelText('도구 민원 제목'), { target: { value: '  가로등 수리 요청  ' } });
    fireEvent.click(within(panel()).getByRole('button', { name: '민원 접수', exact: true }));
    await waitFor(() => expect(mutations(api)).toHaveLength(1));
    expect(mutations(api)[0][0]).toBe('/api/complaints');
    expect(JSON.parse(mutations(api)[0][1].body)).toEqual({ title: '  가로등 수리 요청  ', content: '집 앞 가로등이 꺼져 있습니다.' });
    expect(screen.getByRole('log', { name: '민원 상담 대화' })).toBeTruthy();
  });

  it('opens own complaints from conversation and shows an existing complaint without leaving the chat', async () => {
    const choose = vi.fn();
    const api = toolServer(route('list'));
    render(<Harness api={api} choose={choose} />);
    send('내가 쓴 민원을 확인하고 싶어');
    const select = await screen.findByRole('button', { name: `민원 #42 ${ownComplaint.title} 확인` });
    fireEvent.click(select);
    expect(await within(panel()).findByText(ownComplaint.content)).toBeTruthy();
    expect(screen.getByRole('log', { name: '민원 상담 대화' })).toBeTruthy();
    expect(choose).not.toHaveBeenCalled();
    expect(mutations(api)).toHaveLength(0);
    expect(ragRequests(api)).toHaveLength(0);
  });

  it('opens an edit request as a draft and updates the existing complaint only after saving', async () => {
    const api = toolServer(route('edit', { complaint_id: 42 }));
    render(<Harness api={api} />);
    send('42번 민원을 수정하고 싶어');
    const input = await screen.findByLabelText('도구 민원 내용');
    expect(input.value).toBe(ownComplaint.content);
    expect(mutations(api)).toHaveLength(0);
    fireEvent.change(input, { target: { value: '  파손 구간은 학교 정문 바로 앞입니다.  ' } });
    fireEvent.click(within(panel()).getByRole('button', { name: '수정 저장', exact: true }));
    await waitFor(() => expect(mutations(api)).toHaveLength(1));
    expect(mutations(api)[0][0]).toBe('/api/complaints/42');
    expect(mutations(api)[0][1].method).toBe('PATCH');
    expect(JSON.parse(mutations(api)[0][1].body)).toEqual({ title: ownComplaint.title, content: '  파손 구간은 학교 정문 바로 앞입니다.  ' });
    expect(ragRequests(api)).toHaveLength(0);
  });

  it('preserves unsaved edits when the same complaint is requested again by explicit number', async () => {
    const api = toolServer((body) => route('edit', {
      complaint_id: 42,
      draft: body.message.startsWith('#42') ? { title: '모델이 제안한 다른 제목', content: '모델이 제안한 다른 초안' } : null,
    }));
    render(<Harness api={api} />);
    send('42번 민원을 수정하고 싶어');
    const input = await screen.findByLabelText('도구 민원 내용');
    fireEvent.change(input, { target: { value: '사용자가 직접 작성한 아직 저장하지 않은 수정 내용' } });
    fireEvent.change(within(panel()).getByLabelText('도구 민원 제목'), { target: { value: '사용자가 고친 제목' } });
    send('#42 민원 수정하고 싶어');
    await waitFor(() => expect(screen.getAllByRole('button', { name: '도구 다시 열기', exact: true })).toHaveLength(2));
    expect(within(panel()).getByLabelText('도구 민원 내용').value).toBe('사용자가 직접 작성한 아직 저장하지 않은 수정 내용');
    expect(within(panel()).getByLabelText('도구 민원 제목').value).toBe('사용자가 고친 제목');
    expect(api.mock.calls.filter(([path]) => path === '/api/complaints/42')).toHaveLength(1);
    expect(mutations(api)).toHaveLength(0);
    expect(ragRequests(api)).toHaveLength(0);
  });

  it('never deletes from language alone and allows cancelling before explicit deletion', async () => {
    const api = toolServer(route('delete', { complaint_id: 42 }));
    render(<Harness api={api} />);
    send('42번 민원을 삭제해 줘');
    const dialog = await screen.findByRole('dialog', { name: '민원 삭제 확인' });
    expect(mutations(api)).toHaveLength(0);
    fireEvent.click(within(dialog).getByRole('button', { name: '삭제 취소', exact: true }));
    expect(mutations(api)).toHaveLength(0);
    fireEvent.click(within(panel()).getByRole('button', { name: '민원 삭제', exact: true }));
    fireEvent.click(within(screen.getByRole('dialog', { name: '민원 삭제 확인' })).getByRole('button', { name: '삭제 확인', exact: true }));
    await waitFor(() => expect(mutations(api)).toHaveLength(1));
    expect(mutations(api)[0][0]).toBe('/api/complaints/42');
    expect(mutations(api)[0][1].method).toBe('DELETE');
    expect(ragRequests(api)).toHaveLength(0);
  });

  it('hides and reopens the tools without losing an unsent complaint draft', async () => {
    const api = toolServer();
    render(<Harness api={api} />);
    fireEvent.click(screen.getByRole('button', { name: '민원 도구 열기', exact: true }));
    fireEvent.click(within(panel()).getByRole('button', { name: '새 민원 작성', exact: true }));
    fireEvent.change(within(panel()).getByLabelText('도구 민원 제목'), { target: { value: '저장되지 않은 제목' } });
    fireEvent.change(within(panel()).getByLabelText('도구 민원 내용'), { target: { value: '나중에 이어서 작성할 내용' } });
    fireEvent.click(within(panel()).getByRole('button', { name: '도구 창 닫기', exact: true }));
    expect(screen.queryByRole('region', { name: '민원 도구' })).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: '민원 도구 열기', exact: true }));
    expect(within(panel()).getByLabelText('도구 민원 제목').value).toBe('저장되지 않은 제목');
    expect(within(panel()).getByLabelText('도구 민원 내용').value).toBe('나중에 이어서 작성할 내용');
    expect(mutations(api)).toHaveLength(0);
  });

  it('keeps ordinary questions on the RAG path and preserves prior answer bubbles after opening tools', async () => {
    const api = toolServer((body) => route(body.message.includes('확인하고') ? 'list' : 'answer'));
    render(<Harness api={api} />);
    send('보행로가 파손되면 어디에 신고하나요?');
    await screen.findByText('공식 자료를 확인한 추천 답변입니다. [1]');
    expect(ragRequests(api)).toHaveLength(1);
    expect(screen.queryByRole('region', { name: '민원 도구' })).toBeNull();
    send('내가 쓴 민원을 확인하고 싶어');
    await screen.findByRole('region', { name: '민원 도구' });
    expect(screen.getByText('공식 자료를 확인한 추천 답변입니다. [1]')).toBeTruthy();
    expect(ragRequests(api)).toHaveLength(1);
    expect(mutations(api)).toHaveLength(0);
  });

  it('passes the current tool draft into advice while leaving the unsent complaint untouched', async () => {
    const api = toolServer(route('answer'));
    render(<Harness api={api} />);
    fireEvent.click(screen.getByRole('button', { name: '민원 도구 열기', exact: true }));
    fireEvent.click(within(panel()).getByRole('button', { name: '새 민원 작성', exact: true }));
    fireEvent.change(within(panel()).getByLabelText('도구 민원 제목'), { target: { value: '학교 앞 보행로 파손' } });
    fireEvent.change(within(panel()).getByLabelText('도구 민원 내용'), { target: { value: '학교 정문 앞 인도에 큰 구멍이 있습니다.' } });
    send('이 내용에 어떤 정보를 더 적으면 좋을까요?');
    await screen.findByText('공식 자료를 확인한 추천 답변입니다. [1]');
    const intentCall = api.mock.calls.find(([path]) => path === '/api/ai-recommendations/intent');
    const intentBody = JSON.parse(intentCall[1].body);
    expect(intentBody.message).toBe('이 내용에 어떤 정보를 더 적으면 좋을까요?');
    expect(intentBody.tool_context).toMatchObject({ mode: 'create', title: '학교 앞 보행로 파손', content: '학교 정문 앞 인도에 큰 구멍이 있습니다.' });
    const ragBody = JSON.parse(ragRequests(api)[0][1].body);
    expect(ragBody.content).toContain('학교 정문 앞 인도에 큰 구멍이 있습니다.');
    expect(ragBody.content).toContain('이 내용에 어떤 정보를 더 적으면 좋을까요?');
    expect(within(panel()).getByLabelText('도구 민원 내용').value).toBe('학교 정문 앞 인도에 큰 구멍이 있습니다.');
    expect(mutations(api)).toHaveLength(0);
  });

  it('sends a bounded, backend-valid context from a selected detail without replacing the raw user bubble', async () => {
    const longComplaint = { ...ownComplaint, title: '제'.repeat(250), content: '원'.repeat(5200) };
    const question = '이 민원에 사진을 첨부해야 하나요?';
    const api = toolServer(route('answer'), { rows: [longComplaint] });
    render(<Harness api={api} />);
    fireEvent.click(screen.getByRole('button', { name: '민원 도구 열기', exact: true }));
    fireEvent.click(await screen.findByRole('button', { name: `민원 #42 ${longComplaint.title} 확인` }));
    await within(panel()).findByText(longComplaint.content);
    send(question);
    await screen.findByText('공식 자료를 확인한 추천 답변입니다. [1]');
    const intentBody = JSON.parse(api.mock.calls.find(([path]) => path === '/api/ai-recommendations/intent')[1].body);
    expect(intentBody.tool_context).toEqual({ mode: 'list', complaint_id: 42, title: '제'.repeat(200), content: '원'.repeat(4000) });
    const ragBody = JSON.parse(ragRequests(api)[0][1].body);
    expect(ragBody.title.length).toBeLessThanOrEqual(200);
    expect(ragBody.content).toContain('원'.repeat(4000));
    expect(ragBody.content).not.toContain('원'.repeat(4001));
    const userBubble = screen.getByRole('article', { name: '내 메시지' });
    expect(within(userBubble).getByText(question).textContent).toBe(question);
    expect(userBubble.textContent).not.toContain('민원인이 작성하거나 선택한 내용:');
    expect(mutations(api)).toHaveLength(0);
  });

  it('reopens a create tool from its chat bubble without resetting later draft edits', async () => {
    const api = toolServer(route('create', { draft: { title: '처음 제안된 제목', content: '처음 제안된 초안' } }));
    render(<Harness api={api} />);
    send('새 민원을 작성하고 싶어');
    const title = await screen.findByLabelText('도구 민원 제목');
    fireEvent.change(title, { target: { value: '직접 고친 제목' } });
    fireEvent.change(within(panel()).getByLabelText('도구 민원 내용'), { target: { value: '직접 고친 상세 내용' } });
    fireEvent.click(within(panel()).getByRole('button', { name: '도구 창 닫기', exact: true }));
    expect(screen.queryByRole('region', { name: '민원 도구' })).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: '도구 다시 열기', exact: true }));
    expect(within(panel()).getByLabelText('도구 민원 제목').value).toBe('직접 고친 제목');
    expect(within(panel()).getByLabelText('도구 민원 내용').value).toBe('직접 고친 상세 내용');
    expect(mutations(api)).toHaveLength(0);
  });

  it('shows the list again when requested in chat from an existing complaint detail', async () => {
    const api = toolServer(route('list'));
    render(<Harness api={api} />);
    send('내 민원을 확인하고 싶어');
    fireEvent.click(await screen.findByRole('button', { name: `민원 #42 ${ownComplaint.title} 확인` }));
    await within(panel()).findByText(ownComplaint.content);
    send('내 민원 목록을 다시 보여 줘');
    expect(await within(panel()).findByRole('button', { name: `민원 #42 ${ownComplaint.title} 확인` })).toBeTruthy();
    expect(within(panel()).queryByRole('button', { name: '민원 수정', exact: true })).toBeNull();
    expect(mutations(api)).toHaveLength(0);
  });

  it('shows a routing model failure without silently creating, deleting, or starting RAG', async () => {
    const api = vi.fn().mockRejectedValue(new Error('의도 분류 모델 연결 실패. 잠시 후 다시 시도해 주세요.'));
    render(<Harness api={api} />);
    send('내 민원을 삭제해 줘');
    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toContain('의도 분류 모델 연결 실패');
    expect(screen.getByRole('button', { name: /다시 시도/ })).toBeTruthy();
    expect(api.mock.calls.every(([path]) => path === '/api/ai-recommendations/intent')).toBe(true);
    expect(mutations(api)).toHaveLength(0);
    expect(ragRequests(api)).toHaveLength(0);
  });

  it('allows manual tool submission even when the LLM intent service is unavailable', async () => {
    const toolApi = toolServer();
    const api = vi.fn(async (path, options) => {
      if (path === '/api/ai-recommendations/intent') throw new Error('의도 분류 모델 연결 실패. 민원 도구를 직접 열어 주세요.');
      return toolApi(path, options);
    });
    render(<Harness api={api} />);
    send('민원을 작성하고 싶어');
    await screen.findByRole('alert');
    fireEvent.click(screen.getByRole('button', { name: '민원 도구 열기', exact: true }));
    fireEvent.click(within(panel()).getByRole('button', { name: '새 민원 작성', exact: true }));
    fireEvent.change(within(panel()).getByLabelText('도구 민원 제목'), { target: { value: '직접 접수하는 민원' } });
    fireEvent.change(within(panel()).getByLabelText('도구 민원 내용'), { target: { value: '모델 연결 없이 작성한 민원 내용' } });
    expect(mutations(api)).toHaveLength(0);
    expect(within(panel()).getByRole('button', { name: '민원 접수', exact: true }).disabled).toBe(false);
    fireEvent.click(within(panel()).getByRole('button', { name: '민원 접수', exact: true }));
    await waitFor(() => expect(mutations(api)).toHaveLength(1));
    expect(JSON.parse(mutations(api)[0][1].body)).toEqual({ title: '직접 접수하는 민원', content: '모델 연결 없이 작성한 민원 내용' });
    expect(ragRequests(api)).toHaveLength(0);
  });

  it('retains the edit draft when the server rejects an obsolete complaint status', async () => {
    const message = '접수 대기 상태의 민원만 수정할 수 있습니다.';
    const api = toolServer(route('edit', { complaint_id: 42 }), { patchFailure: message });
    render(<Harness api={api} />);
    send('42번 민원을 수정하고 싶어');
    const input = await screen.findByLabelText('도구 민원 내용');
    fireEvent.change(input, { target: { value: '이미 작성한 수정 초안' } });
    fireEvent.click(within(panel()).getByRole('button', { name: '수정 저장', exact: true }));
    expect(await within(panel()).findByText(message)).toBeTruthy();
    expect(within(panel()).getByLabelText('도구 민원 내용').value).toBe('이미 작성한 수정 초안');
    expect(mutations(api)).toHaveLength(1);
  });

  it('preserves the App page when the existing-complaint option opens the side tools', async () => {
    sessionStorage.setItem('complaintai.fastapi.auth', JSON.stringify({ token: 'test-token', user: { role: 'user', name: '테스트 민원인' } }));
    vi.stubGlobal('fetch', vi.fn(async (url) => {
      const path = new URL(url).pathname;
      const body = path.endsWith('/counts') ? { categories: [] }
        : path === '/api/complaints' ? { complaints: [ownComplaint], total: 1 }
          : { knowledge_ready: true };
      return new Response(JSON.stringify(body), { status: 200 });
    }));
    render(<App />);
    fireEvent.click(screen.getByRole('button', { name: 'AI 추천 답변', exact: true }));
    fireEvent.click(screen.getByLabelText('대화 옵션'));
    fireEvent.click(screen.getByRole('button', { name: /작성된 민원에서 선택하기/ }));
    await screen.findByRole('button', { name: `민원 #42 ${ownComplaint.title} 확인` });
    expect(screen.getByRole('heading', { name: 'AI 추천 답변', exact: true })).toBeTruthy();
    expect(screen.getByRole('log', { name: '민원 상담 대화' })).toBeTruthy();
    expect(screen.queryByRole('heading', { name: '작성한 민원', exact: true })).toBeNull();
  });
});
