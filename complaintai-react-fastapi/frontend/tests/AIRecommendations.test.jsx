import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import AIRecommendations, { useRecommendations } from '../src/AIRecommendations.jsx';
import App from '../src/App.jsx';

afterEach(() => { cleanup(); sessionStorage.clear(); localStorage.clear(); vi.unstubAllGlobals(); });

function Harness({ api, choose = vi.fn() }) {
  const recommendation = useRecommendations(api);
  return <AIRecommendations recommendation={recommendation} onChooseExisting={choose} />;
}
const openOptions = () => fireEvent.click(screen.getByLabelText('대화 옵션'));
const completed = (id, content, answer = '공식 안내에 따른 답변입니다. [1]') => ({
  id, status: 'completed', title: '상담 제목', content,
  result: { answer_status: 'answered', answer, checks: [], next_steps: [], citations: [] },
});
const answerIntent = { intent: 'answer', message: '', complaint_id: null, draft: null };
const answerServer = (rag) => vi.fn(async (path, options) => path === '/api/ai-recommendations/intent' ? answerIntent : rag(path, options));

describe('AI 추천 답변 대화 화면', () => {
  it('starts with a chat composer and opens auxiliary actions beside the conversation', async () => {
    const choose = vi.fn();
    render(<Harness api={vi.fn().mockResolvedValue({ complaints: [], total: 0 })} choose={choose} />);
    expect(screen.getByRole('log', { name: '민원 상담 대화' })).toBeTruthy();
    expect(screen.getByLabelText('메시지')).toBeTruthy();
    expect(screen.getByLabelText('대화 옵션').parentElement.open).toBe(false);
    openOptions();
    fireEvent.click(screen.getByRole('button', { name: /작성된 민원에서 선택하기/ }));
    expect(await screen.findByRole('region', { name: '민원 도구' })).toBeTruthy();
    expect(screen.getByRole('log', { name: '민원 상담 대화' })).toBeTruthy();
    expect(choose).not.toHaveBeenCalled();
  });

  it('posts a chat message once, then polls and shows the reply in the same conversation', async () => {
    const api = answerServer(async (path, options) => options?.method === 'POST'
      ? { id: 'job-1', status: 'queued', stage: 'queued', title: '아파트 층간소음', content: '아파트 층간소음' }
      : completed('job-1', '아파트 층간소음'));
    render(<Harness api={api} />);
    const button = screen.getByRole('button', { name: '전송' });
    expect(button.disabled).toBe(true);
    fireEvent.change(screen.getByLabelText('메시지'), { target: { value: '아파트 층간소음' } });
    fireEvent.click(button); fireEvent.click(button);
    const posts = () => api.mock.calls.filter(([path, options]) => path === '/api/ai-recommendations' && options?.method === 'POST');
    await waitFor(() => expect(posts()).toHaveLength(1));
    expect(JSON.parse(posts()[0][1].body)).toEqual({ title: '아파트 층간소음', content: '아파트 층간소음' });
    expect(screen.getByLabelText('메시지').disabled).toBe(true);
    await waitFor(() => expect(screen.getByText('공식 안내에 따른 답변입니다. [1]')).toBeTruthy());
    expect(screen.getByText('아파트 층간소음')).toBeTruthy();
    expect(screen.getByLabelText('메시지').value).toBe('');
    expect(screen.getByLabelText('메시지').disabled).toBe(false);
  });

  it('keeps previous bubbles and connects follow-up questions to the completed job', async () => {
    const rag = vi.fn()
      .mockResolvedValueOnce(completed('first-job', '아파트에서 소음이 납니다.', '먼저 상담을 신청해 주세요. [1]'))
      .mockResolvedValueOnce(completed('second-job', '어디에 신청하나요?', '센터의 상담 절차를 이용해 주세요. [1]'));
    const api = answerServer(rag);
    render(<Harness api={api} />);
    fireEvent.change(screen.getByLabelText('메시지'), { target: { value: '아파트에서 소음이 납니다.' } });
    fireEvent.click(screen.getByRole('button', { name: '전송' }));
    await screen.findByText('먼저 상담을 신청해 주세요. [1]');
    fireEvent.change(screen.getByLabelText('메시지'), { target: { value: '어디에 신청하나요?' } });
    fireEvent.click(screen.getByRole('button', { name: '전송' }));
    await screen.findByText('센터의 상담 절차를 이용해 주세요. [1]');
    expect(JSON.parse(rag.mock.calls[1][1].body)).toEqual({ parent_recommendation_id: 'first-job', content: '어디에 신청하나요?' });
    expect(screen.getByText('먼저 상담을 신청해 주세요. [1]')).toBeTruthy();
    expect(screen.getAllByRole('article', { name: '내 메시지' })).toHaveLength(2);
  });

  it('sends with Enter while preserving Shift+Enter and Korean IME composition', async () => {
    const api = answerServer(async () => completed('job-keyboard', '테스트 질문'));
    render(<Harness api={api} />);
    const input = screen.getByLabelText('메시지');
    fireEvent.change(input, { target: { value: '테스트 질문' } });
    fireEvent.keyDown(input, { key: 'Enter', shiftKey: true });
    fireEvent.compositionStart(input);
    fireEvent.keyDown(input, { key: 'Enter', keyCode: 229 });
    fireEvent.keyDown(input, { key: 'Enter', isComposing: true });
    expect(api).not.toHaveBeenCalled();
    fireEvent.compositionEnd(input);
    fireEvent.keyDown(input, { key: 'Enter' });
    await waitFor(() => expect(api).toHaveBeenCalledTimes(2));
  });

  it('supports real complaint submission through the tool form in options', async () => {
    const api = vi.fn(async (path, options = {}) => path === '/api/complaints' && options.method === 'POST'
      ? { complaint: { id: 44, title: '테스트 제목', content: '테스트 내용', complaint_status: '접수', submitted_by_user: true }, message: '민원이 접수되었습니다.' }
      : path.startsWith('/api/complaints?') ? { complaints: [], total: 0 }
        : { complaint: { id: 44, title: '테스트 제목', content: '테스트 내용', complaint_status: '접수', submitted_by_user: true } });
    render(<Harness api={api} />);
    openOptions();
    fireEvent.click(screen.getByRole('button', { name: /새로운 민원 작성하기/ }));
    expect(screen.getByRole('button', { name: '민원 접수', exact: true }).disabled).toBe(true);
    fireEvent.change(screen.getByLabelText('도구 민원 제목'), { target: { value: '테스트 제목' } });
    fireEvent.change(screen.getByLabelText('도구 민원 내용'), { target: { value: '테스트 내용' } });
    fireEvent.click(screen.getByRole('button', { name: '민원 접수', exact: true }));
    await waitFor(() => expect(api.mock.calls.filter(([, options]) => options?.method === 'POST')).toHaveLength(1));
    const posted = api.mock.calls.find(([, options]) => options?.method === 'POST');
    expect(posted[0]).toBe('/api/complaints');
    expect(JSON.parse(posted[1].body)).toEqual({ title: '테스트 제목', content: '테스트 내용' });
    expect(screen.getByLabelText('메시지')).toBeTruthy();
  });

  it('shows answer extras only when expanded and renders source text safely', () => {
    const job = completed('job-result', '내용');
    job.result.checks = ['주택 종류 확인'];
    job.result.next_steps = ['상담 신청'];
    job.result.citations = [{ number: 1, title: '공식 FAQ', locator: 'Q1', publisher: '공공기관', kind: 'FAQ', version: '2026', excerpt: '<script>unsafe</script> 원문 근거', url: 'https://example.gov/faq', retrieved_at: '2026-10-07T00:00:00Z' }];
    const recommendation = { messages: [{ id: 'a', role: 'assistant', job }], draft: '', setDraft: vi.fn(), busy: false, reset: vi.fn(), start: vi.fn(), send: vi.fn() };
    const { container } = render(<AIRecommendations recommendation={recommendation} onChooseExisting={vi.fn()} />);
    expect(screen.getByText('공식 안내에 따른 답변입니다. [1]')).toBeTruthy();
    expect(screen.getByText('답변 옵션').parentElement.open).toBe(false);
    fireEvent.click(screen.getByText('답변 옵션'));
    expect(screen.getByText('주택 종류 확인')).toBeTruthy();
    fireEvent.click(screen.getByText('[1] 공식 FAQ · Q1'));
    expect(screen.getByRole('link', { name: /공식 원문 보기/ }).getAttribute('href')).toBe('https://example.gov/faq');
    expect(container.querySelector('script')).toBeNull();
    expect(screen.getByText('<script>unsafe</script> 원문 근거')).toBeTruthy();
  });

  it('retries a failed message without duplicating user bubbles and can start a fresh conversation', async () => {
    const rag = vi.fn().mockRejectedValueOnce(new Error('근거 검토 모델 연결 실패')).mockResolvedValueOnce(completed('job-retry', '다시 확인해 주세요'));
    const api = answerServer(rag);
    render(<Harness api={api} />);
    fireEvent.change(screen.getByLabelText('메시지'), { target: { value: '다시 확인해 주세요' } });
    fireEvent.click(screen.getByRole('button', { name: '전송' }));
    await screen.findByRole('alert');
    fireEvent.click(screen.getByRole('button', { name: '다시 시도' }));
    await screen.findByText('공식 안내에 따른 답변입니다. [1]');
    expect(screen.getAllByRole('article', { name: '내 메시지' })).toHaveLength(1);
    expect(JSON.parse(rag.mock.calls[0][1].body)).toEqual(JSON.parse(rag.mock.calls[1][1].body));
    openOptions();
    fireEvent.click(screen.getByRole('button', { name: '새 대화 시작' }));
    expect(screen.queryByText('공식 안내에 따른 답변입니다. [1]')).toBeNull();
    expect(screen.queryByRole('article', { name: '내 메시지' })).toBeNull();
  });

  it('selects an existing complaint and preserves the chat while navigating other menus', async () => {
    sessionStorage.setItem('complaintai.fastapi.auth', JSON.stringify({ token: 'test-token', user: { role: 'user', name: '테스트 민원인' } }));
    const row = { id: 42, title: '층간소음 민원', content: '저장된 원문', complaint_status: '접수', submitted_by_user: true, created_at: '2026-10-07' };
    const calls = [];
    vi.stubGlobal('fetch', vi.fn(async (url, options = {}) => {
      calls.push([url, options]);
      const path = new URL(url).pathname;
      const body = path.endsWith('/counts') ? { categories: [] } : path === '/api/complaints' ? { complaints: [row], total: 1 } : path === '/api/complaints/42' ? { complaint: row } : path === '/api/ai-recommendations'
        ? { ...completed('job-existing', row.content), title: row.title, complaint_id: 42 } : { knowledge_ready: true };
      return new Response(JSON.stringify(body), { status: 200 });
    }));
    render(<App />);
    fireEvent.click(screen.getByRole('button', { name: 'AI 추천 답변', exact: true }));
    openOptions();
    fireEvent.click(screen.getByRole('button', { name: /작성된 민원에서 선택하기/ }));
    const panel = await screen.findByRole('region', { name: '민원 도구' });
    fireEvent.click(await within(panel).findByRole('button', { name: `민원 #42 ${row.title} 확인` }));
    fireEvent.click(await within(panel).findByRole('button', { name: '이 민원으로 AI 답변 보기', exact: true }));
    await screen.findByText('공식 안내에 따른 답변입니다. [1]');
    const sent = calls.find(([url]) => new URL(url).pathname === '/api/ai-recommendations');
    expect(JSON.parse(sent[1].body)).toEqual({ complaint_id: 42 });
    expect(screen.getAllByText('저장된 원문').length).toBeGreaterThan(0);
    fireEvent.click(screen.getByRole('button', { name: '새 민원', exact: true }));
    fireEvent.click(screen.getByRole('button', { name: 'AI 추천 답변', exact: true }));
    expect(screen.getByText('공식 안내에 따른 답변입니다. [1]')).toBeTruthy();
  });
});

const citizenSearchRow = {
  id: 91, title: '아파트 층간소음 상담', content: '위층에서 반복되는 소음 때문에 상담을 요청합니다.',
  category: '주택·건축', complaint_status: '접수', submitted_by_user: true, created_at: '2026-10-14', deleted_at: null,
};
const citizenSimilarRow = { ...citizenSearchRow, id: 92, title: '공동주택 소음 측정 문의' };
const chatDecision = (intent) => ({ intent, message: '민원 도구에서 확인해 주세요.', complaint_id: null, draft: null });
const searchResponse = (query, complaints = [citizenSearchRow], similar = [citizenSimilarRow]) => ({
  query, complaints, similar_complaints: similar, total: complaints.length, similar_total: similar.length,
});
const sendChatMessage = (content) => {
  fireEvent.change(screen.getByLabelText('메시지'), { target: { value: content } });
  fireEvent.click(screen.getByRole('button', { name: '전송', exact: true }));
};
const searchPosts = (api) => api.mock.calls.filter(([path, options]) => path === '/api/ai-recommendations/complaint-search' && options?.method === 'POST');
const intentPosts = (api) => api.mock.calls.filter(([path]) => path === '/api/ai-recommendations/intent');
const recommendationPosts = (api) => api.mock.calls.filter(([path, options]) => path === '/api/ai-recommendations' && options?.method === 'POST');
function searchChatApi({ decide = () => 'answer', search = (query) => searchResponse(query) } = {}) {
  return vi.fn(async (path, options = {}) => {
    const body = options.body ? JSON.parse(options.body) : null;
    if (path === '/api/ai-recommendations/intent') return chatDecision(decide(body.message));
    if (path === '/api/ai-recommendations/complaint-search') return search(body.query);
    if (path === '/api/ai-recommendations') return completed('search-followup-job', body.content);
    if (path.startsWith('/api/complaints?')) return { complaints: [citizenSearchRow], total: 1 };
    if (path === '/api/complaints/91') return { complaint: citizenSearchRow };
    throw new Error(`예상하지 않은 API 요청: ${path}`);
  });
}

describe('대화에서 민원 조회 조건과 검색', () => {
  it('asks for department and status selections before opening a filtered tool from a natural request', async () => {
    const api = searchChatApi({ decide: () => 'filter' });
    render(<Harness api={api} />);
    sendChatMessage('특정 민원들을 조회하고 싶어요');
    const card = await screen.findByRole('region', { name: '민원 조회 조건' });
    expect(screen.getAllByText('어떤 민원들을 조회하시겠습니까?').length).toBeGreaterThan(0);
    expect(within(card).getAllByRole('checkbox')).toHaveLength(7);
    expect(within(card).getAllByRole('radio')).toHaveLength(3);
    const apply = within(card).getByRole('button', { name: '조회하기', exact: true });
    expect(apply.disabled).toBe(true);
    expect(screen.queryByRole('region', { name: '민원 도구' })).toBeNull();
    fireEvent.click(within(card).getByRole('checkbox', { name: '주택·건축', exact: true }));
    expect(apply.disabled).toBe(true);
    fireEvent.click(within(card).getByRole('checkbox', { name: '환경·위생', exact: true }));
    fireEvent.click(within(card).getByRole('radio', { name: '접수 중', exact: true }));
    expect(apply.disabled).toBe(false);
    fireEvent.click(apply);
    await screen.findByRole('region', { name: '민원 도구' });
    await waitFor(() => {
      const filtered = api.mock.calls.find(([path]) => new URL(path, 'http://test.local').searchParams.get('view_status') === 'in_progress');
      expect(filtered).toBeTruthy();
      expect(new URL(filtered[0], 'http://test.local').searchParams.getAll('departments')).toEqual(['주택·건축', '환경·위생']);
    });
    expect(searchPosts(api)).toHaveLength(0);
    expect(recommendationPosts(api)).toHaveLength(0);
  });

  it('opens the same filter card from options without calling the intent model', async () => {
    const api = searchChatApi();
    render(<Harness api={api} />);
    openOptions();
    fireEvent.click(screen.getByRole('button', { name: /특정 민원 조회/ }));
    const card = await screen.findByRole('region', { name: '민원 조회 조건' });
    expect(screen.getAllByText('어떤 민원들을 조회하시겠습니까?').length).toBeGreaterThan(0);
    const apply = within(card).getByRole('button', { name: '조회하기', exact: true });
    fireEvent.click(within(card).getByRole('radio', { name: '접수 완료', exact: true }));
    expect(apply.disabled).toBe(true);
    fireEvent.click(within(card).getByRole('checkbox', { name: '교통·국토', exact: true }));
    expect(apply.disabled).toBe(false);
    fireEvent.click(within(card).getByRole('button', { name: '조건 초기화', exact: true }));
    expect(apply.disabled).toBe(true);
    expect(within(card).getAllByRole('checkbox').every((element) => !element.checked)).toBe(true);
    expect(within(card).getAllByRole('radio').every((element) => !element.checked)).toBe(true);
    expect(intentPosts(api)).toHaveLength(0);
    expect(recommendationPosts(api)).toHaveLength(0);
  });

  it('uses only the next message as a search query and routes the following message normally', async () => {
    const api = searchChatApi({ decide: (message) => message.includes('검색하고') ? 'search' : 'answer' });
    render(<Harness api={api} />);
    sendChatMessage('내 민원을 검색하고 싶어');
    await screen.findByText(/^어떤 민원을 검색하시겠습니까\?/);
    expect(searchPosts(api)).toHaveLength(0);
    sendChatMessage('아파트 층간소음');
    await screen.findByRole('heading', { name: '검색된 민원', exact: true });
    await screen.findByRole('button', { name: `민원 #91 ${citizenSearchRow.title} 확인` });
    expect(screen.getByRole('heading', { name: '검색과 유사한 민원', exact: true })).toBeTruthy();
    expect(screen.getByRole('button', { name: `민원 #92 ${citizenSimilarRow.title} 확인` })).toBeTruthy();
    expect(searchPosts(api)).toHaveLength(1);
    expect(JSON.parse(searchPosts(api)[0][1].body)).toEqual({ query: '아파트 층간소음' });
    expect(intentPosts(api)).toHaveLength(1);
    expect(recommendationPosts(api)).toHaveLength(0);
    sendChatMessage('층간소음 상담은 어떻게 진행하나요?');
    await screen.findByText('공식 안내에 따른 답변입니다. [1]');
    expect(searchPosts(api)).toHaveLength(1);
    expect(intentPosts(api)).toHaveLength(2);
    expect(recommendationPosts(api)).toHaveLength(1);
    expect(screen.getAllByRole('article', { name: '내 메시지' })).toHaveLength(3);
  });

  it('opens the search prompt directly from options and preserves Korean composition before consuming the query', async () => {
    const api = searchChatApi();
    render(<Harness api={api} />);
    openOptions();
    fireEvent.click(screen.getByRole('button', { name: /^민원 검색/ }));
    await screen.findByText(/^어떤 민원을 검색하시겠습니까\?/);
    const input = screen.getByLabelText('메시지');
    fireEvent.change(input, { target: { value: '학교 앞 도로 파손' } });
    fireEvent.keyDown(input, { key: 'Enter', shiftKey: true });
    fireEvent.compositionStart(input);
    fireEvent.keyDown(input, { key: 'Enter', keyCode: 229 });
    fireEvent.keyDown(input, { key: 'Enter', isComposing: true });
    expect(searchPosts(api)).toHaveLength(0);
    fireEvent.compositionEnd(input);
    fireEvent.keyDown(input, { key: 'Enter' });
    await waitFor(() => expect(searchPosts(api)).toHaveLength(1));
    expect(JSON.parse(searchPosts(api)[0][1].body)).toEqual({ query: '학교 앞 도로 파손' });
    expect(intentPosts(api)).toHaveLength(0);
    expect(recommendationPosts(api)).toHaveLength(0);
  });

  it('opens the search tool immediately while the real search request is pending and ignores duplicate sends', async () => {
    let resolveSearch;
    const pending = new Promise((resolve) => { resolveSearch = resolve; });
    const api = searchChatApi({ search: () => pending });
    render(<Harness api={api} />);
    openOptions();
    fireEvent.click(screen.getByRole('button', { name: /^민원 검색/ }));
    sendChatMessage('보행로 파손');
    fireEvent.click(screen.getByRole('button', { name: '전송', exact: true }));
    const tools = await screen.findByRole('region', { name: '민원 도구' });
    expect(within(tools).getByText('관련 민원을 검색하고 있습니다.')).toBeTruthy();
    expect(screen.getByLabelText('메시지').disabled).toBe(true);
    expect(searchPosts(api)).toHaveLength(1);
    resolveSearch(searchResponse('보행로 파손'));
    await screen.findByRole('button', { name: `민원 #91 ${citizenSearchRow.title} 확인` });
    expect(screen.getByLabelText('메시지').disabled).toBe(false);
    expect(searchPosts(api)).toHaveLength(1);
    expect(recommendationPosts(api)).toHaveLength(0);
  });

  it('retries a failed search with the same query without rerouting or duplicating the user bubble', async () => {
    let resolveRetry;
    const pendingRetry = new Promise((resolve) => { resolveRetry = resolve; });
    const search = vi.fn().mockRejectedValueOnce(new Error('검색 모델 연결 실패 (503)')).mockImplementationOnce(() => pendingRetry);
    const api = searchChatApi({ search });
    render(<Harness api={api} />);
    openOptions();
    fireEvent.click(screen.getByRole('button', { name: /^민원 검색/ }));
    sendChatMessage('학교 주변 불법 주차');
    await screen.findAllByRole('alert');
    const failedBubble = screen.getAllByRole('article', { name: 'AI 답변' }).find((article) => within(article).queryByRole('alert'));
    expect(within(failedBubble).getByRole('alert').textContent).toContain('검색 모델 연결 실패 (503)');
    const retry = within(failedBubble).getByRole('button', { name: '다시 시도', exact: true });
    fireEvent.click(retry); fireEvent.click(retry);
    await waitFor(() => expect(searchPosts(api)).toHaveLength(2));
    expect(searchPosts(api).map(([, options]) => JSON.parse(options.body))).toEqual([{ query: '학교 주변 불법 주차' }, { query: '학교 주변 불법 주차' }]);
    expect(intentPosts(api)).toHaveLength(0);
    expect(recommendationPosts(api)).toHaveLength(0);
    resolveRetry(searchResponse('학교 주변 불법 주차'));
    await screen.findByRole('button', { name: `민원 #91 ${citizenSearchRow.title} 확인` });
    expect(screen.getAllByRole('article', { name: '내 메시지' })).toHaveLength(1);
    expect(screen.getByLabelText('메시지').disabled).toBe(false);
  });

  it('keeps an empty search as an empty result and returns the following message to normal advice', async () => {
    const api = searchChatApi({ search: (query) => searchResponse(query, [], []) });
    render(<Harness api={api} />);
    openOptions();
    fireEvent.click(screen.getByRole('button', { name: /^민원 검색/ }));
    sendChatMessage('아직 작성한 적 없는 민원');
    await screen.findByRole('heading', { name: '검색된 민원', exact: true });
    expect(await screen.findByRole('heading', { name: '검색과 유사한 민원', exact: true })).toBeTruthy();
    const tools = screen.getByRole('region', { name: '민원 도구' });
    expect(within(tools).queryByRole('button', { name: /민원 #\d+ .* 확인/ })).toBeNull();
    expect(screen.getByLabelText('메시지').disabled).toBe(false);
    sendChatMessage('민원은 어떻게 작성하면 되나요?');
    await screen.findByText('공식 안내에 따른 답변입니다. [1]');
    expect(searchPosts(api)).toHaveLength(1);
    expect(intentPosts(api)).toHaveLength(1);
    expect(recommendationPosts(api)).toHaveLength(1);
  });

  it.each(['검색 취소', '새 대화 시작', '작성된 민원에서 선택하기', '특정 민원 조회'])('does not consume a search query after %s changes the active mode', async (action) => {
    const api = searchChatApi();
    render(<Harness api={api} />);
    openOptions();
    fireEvent.click(screen.getByRole('button', { name: /^민원 검색/ }));
    await screen.findByText(/^어떤 민원을 검색하시겠습니까\?/);
    if (action === '검색 취소') fireEvent.click(screen.getByRole('button', { name: action, exact: true }));
    else {
      openOptions();
      fireEvent.click(screen.getByRole('button', { name: new RegExp(`^${action}`) }));
    }
    sendChatMessage('보도 파손은 어디에 신고하나요?');
    await screen.findByText('공식 안내에 따른 답변입니다. [1]');
    expect(searchPosts(api)).toHaveLength(0);
    expect(intentPosts(api)).toHaveLength(1);
    expect(recommendationPosts(api)).toHaveLength(1);
  });
});
