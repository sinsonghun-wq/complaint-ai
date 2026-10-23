import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import AIRecommendations, { requestChat, useRecommendations } from '../src/AIRecommendations.jsx';

afterEach(cleanup);
function Harness({ api }) {
  const recommendation = useRecommendations(api);
  return <AIRecommendations recommendation={recommendation} />;
}
function send(text) {
  fireEvent.change(screen.getByLabelText('메시지'), { target: { value: text } });
  fireEvent.click(screen.getByRole('button', { name: '전송', exact: true }));
}

describe('doc-main의 기존 AI 채팅 계약 유지', () => {
  it('uses /api/chat and keeps consecutive answers visible', async () => {
    let index = 0;
    const api = vi.fn(async () => ({ code: 0, message: `기존 AI 답변 ${++index}`, result: [] }));
    render(<Harness api={api} />);
    send('첫 번째 질문');
    await screen.findByText('기존 AI 답변 1');
    send('두 번째 질문');
    await screen.findByText('기존 AI 답변 2');
    expect(screen.getByText('기존 AI 답변 1')).toBeTruthy();
    expect(api.mock.calls.map(([path]) => path)).toEqual(['/api/chat', '/api/chat']);
    expect(JSON.parse(api.mock.calls[0][1].body)).toEqual({ content: '첫 번째 질문', agent_mode: 0 });
  });

  it('opens the citizen tool from the current code=1 reply without submitting a complaint', async () => {
    const api = vi.fn(async (path) => path === '/api/chat'
      ? { code: 1, message: '민원 작성 도구를 열었습니다.', result: [{ title: '도로 보수', content: '도로를 고쳐 주세요.' }] }
      : { complaints: [], total: 0 });
    render(<Harness api={api} />);
    send('민원을 작성하고 싶어요.');
    await screen.findByRole('region', { name: '민원 도구' });
    expect(api.mock.calls.some(([path, options]) => path === '/api/complaints' && options?.method === 'POST')).toBe(false);
    await screen.findByText('민원 작성 도구를 열었습니다.');
  });

  it('shows errors and allows retry without replacing the AI implementation', async () => {
    const api = vi.fn().mockRejectedValueOnce(new Error('연결 확인용 오류')).mockResolvedValue({ code: 0, message: '다시 연결됐습니다.', result: [] });
    render(<Harness api={api} />);
    send('연결 확인');
    await screen.findByText('연결 확인용 오류');
    fireEvent.click(screen.getByRole('button', { name: '다시 시도' }));
    await waitFor(() => expect(screen.getByText('다시 연결됐습니다.')).toBeTruthy());
  });

  it('stops a hung request at the deadline even when the network promise never resolves', async () => {
    const controller = new AbortController();
    const api = vi.fn(() => new Promise(() => {}));
    await expect(requestChat(api, { content: '질문', agent_mode: 1 }, controller, 5)).rejects.toThrow('대기 시간이 초과');
    expect(controller.signal.aborted).toBe(true);
  });

  it('keeps a request open past the old deadline and allows the backend twenty minutes', async () => {
    vi.useFakeTimers();
    try {
      const controller = new AbortController();
      const pending = requestChat(() => new Promise(() => {}), { content: '질문', agent_mode: 1 }, controller);
      const timeout = expect(pending).rejects.toThrow('대기 시간이 초과');
      await vi.advanceTimersByTimeAsync(190000);
      expect(controller.signal.aborted).toBe(false);
      await vi.advanceTimersByTimeAsync(1010000);
      expect(controller.signal.aborted).toBe(false);
      await vi.advanceTimersByTimeAsync(10000);
      await timeout;
      expect(controller.signal.reason).toBe('timeout');
    } finally {
      vi.useRealTimers();
    }
  });

  it('lets the user cancel loading and ignores an old reply after retry', async () => {
    let finish;
    const api = vi.fn().mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }))
      .mockResolvedValue({ code: 0, message: '새 답변', result: [] });
    render(<Harness api={api} />);
    send('오래 걸리는 질문');
    fireEvent.click(await screen.findByRole('button', { name: '답변 요청 취소' }));
    await screen.findByText('답변 요청을 취소했습니다.');
    expect(screen.getByLabelText('메시지').disabled).toBe(false);
    send('다음 질문');
    await screen.findByText('새 답변');
    finish({ code: 0, message: '늦게 도착한 답변', result: [] });
    await waitFor(() => expect(screen.queryByText('늦게 도착한 답변')).toBeNull());
  });
});
