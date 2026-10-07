import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
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

describe('AI 추천 답변 대화 화면', () => {
  it('starts with a chat composer and moves auxiliary actions into options', () => {
    const choose = vi.fn();
    render(<Harness api={vi.fn()} choose={choose} />);
    expect(screen.getByRole('log', { name: '민원 상담 대화' })).toBeTruthy();
    expect(screen.getByLabelText('메시지')).toBeTruthy();
    expect(screen.getByLabelText('대화 옵션').parentElement.open).toBe(false);
    openOptions();
    fireEvent.click(screen.getByRole('button', { name: /작성된 민원에서 선택하기/ }));
    expect(choose).toHaveBeenCalledTimes(1);
  });

  it('posts a chat message once, then polls and shows the reply in the same conversation', async () => {
    const api = vi.fn(async (path, options) => options?.method === 'POST'
      ? { id: 'job-1', status: 'queued', stage: 'queued', title: '아파트 층간소음', content: '아파트 층간소음' }
      : completed('job-1', '아파트 층간소음'));
    render(<Harness api={api} />);
    const button = screen.getByRole('button', { name: '전송' });
    expect(button.disabled).toBe(true);
    fireEvent.change(screen.getByLabelText('메시지'), { target: { value: '아파트 층간소음' } });
    fireEvent.click(button); fireEvent.click(button);
    const posts = () => api.mock.calls.filter(([, options]) => options?.method === 'POST');
    await waitFor(() => expect(posts()).toHaveLength(1));
    expect(JSON.parse(posts()[0][1].body)).toEqual({ title: '아파트 층간소음', content: '아파트 층간소음' });
    expect(screen.getByLabelText('메시지').disabled).toBe(true);
    await waitFor(() => expect(screen.getByText('공식 안내에 따른 답변입니다. [1]')).toBeTruthy());
    expect(screen.getByText('아파트 층간소음')).toBeTruthy();
    expect(screen.getByLabelText('메시지').value).toBe('');
    expect(screen.getByLabelText('메시지').disabled).toBe(false);
  });

  it('keeps previous bubbles and connects follow-up questions to the completed job', async () => {
    const api = vi.fn()
      .mockResolvedValueOnce(completed('first-job', '아파트에서 소음이 납니다.', '먼저 상담을 신청해 주세요. [1]'))
      .mockResolvedValueOnce(completed('second-job', '어디에 신청하나요?', '센터의 상담 절차를 이용해 주세요. [1]'));
    render(<Harness api={api} />);
    fireEvent.change(screen.getByLabelText('메시지'), { target: { value: '아파트에서 소음이 납니다.' } });
    fireEvent.click(screen.getByRole('button', { name: '전송' }));
    await screen.findByText('먼저 상담을 신청해 주세요. [1]');
    fireEvent.change(screen.getByLabelText('메시지'), { target: { value: '어디에 신청하나요?' } });
    fireEvent.click(screen.getByRole('button', { name: '전송' }));
    await screen.findByText('센터의 상담 절차를 이용해 주세요. [1]');
    expect(JSON.parse(api.mock.calls[1][1].body)).toEqual({ parent_recommendation_id: 'first-job', content: '어디에 신청하나요?' });
    expect(screen.getByText('먼저 상담을 신청해 주세요. [1]')).toBeTruthy();
    expect(screen.getAllByRole('article', { name: '내 메시지' })).toHaveLength(2);
  });

  it('sends with Enter while preserving Shift+Enter and Korean IME composition', async () => {
    const api = vi.fn().mockResolvedValue(completed('job-keyboard', '테스트 질문'));
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
    await waitFor(() => expect(api).toHaveBeenCalledTimes(1));
  });

  it('supports the title/content form through options', async () => {
    const api = vi.fn().mockResolvedValue(completed('job-form', '테스트 내용'));
    render(<Harness api={api} />);
    openOptions();
    fireEvent.click(screen.getByRole('button', { name: /새로운 민원 작성하기/ }));
    expect(screen.getByRole('button', { name: '대화 시작' }).disabled).toBe(true);
    fireEvent.change(screen.getByLabelText('민원 제목'), { target: { value: '테스트 제목' } });
    fireEvent.change(screen.getByLabelText('민원 내용'), { target: { value: '테스트 내용' } });
    fireEvent.click(screen.getByRole('button', { name: '대화 시작' }));
    await waitFor(() => expect(api).toHaveBeenCalledTimes(1));
    expect(JSON.parse(api.mock.calls[0][1].body)).toEqual({ title: '테스트 제목', content: '테스트 내용' });
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
    const api = vi.fn().mockRejectedValueOnce(new Error('근거 검토 모델 연결 실패')).mockResolvedValueOnce(completed('job-retry', '다시 확인해 주세요'));
    render(<Harness api={api} />);
    fireEvent.change(screen.getByLabelText('메시지'), { target: { value: '다시 확인해 주세요' } });
    fireEvent.click(screen.getByRole('button', { name: '전송' }));
    await screen.findByRole('alert');
    fireEvent.click(screen.getByRole('button', { name: '다시 시도' }));
    await screen.findByText('공식 안내에 따른 답변입니다. [1]');
    expect(screen.getAllByRole('article', { name: '내 메시지' })).toHaveLength(1);
    expect(JSON.parse(api.mock.calls[0][1].body)).toEqual(JSON.parse(api.mock.calls[1][1].body));
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
      const body = path.endsWith('/counts') ? { categories: [] } : path === '/api/complaints' ? { complaints: [row], total: 1 } : path === '/api/ai-recommendations'
        ? { ...completed('job-existing', row.content), title: row.title, complaint_id: 42 } : { knowledge_ready: true };
      return new Response(JSON.stringify(body), { status: 200 });
    }));
    render(<App />);
    fireEvent.click(screen.getByRole('button', { name: 'AI 추천 답변', exact: true }));
    openOptions();
    fireEvent.click(screen.getByRole('button', { name: /작성된 민원에서 선택하기/ }));
    await waitFor(() => expect(screen.getByRole('button', { name: '이 민원 선택' })).toBeTruthy());
    expect(screen.queryByRole('button', { name: '삭제', exact: true })).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: '이 민원 선택' }));
    await screen.findByText('공식 안내에 따른 답변입니다. [1]');
    const sent = calls.find(([url]) => new URL(url).pathname === '/api/ai-recommendations');
    expect(JSON.parse(sent[1].body)).toEqual({ complaint_id: 42 });
    expect(screen.getByText('저장된 원문')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: '새 민원', exact: true }));
    fireEvent.click(screen.getByRole('button', { name: 'AI 추천 답변', exact: true }));
    expect(screen.getByText('공식 안내에 따른 답변입니다. [1]')).toBeTruthy();
  });
});
