import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import App from '../src/App.jsx';

afterEach(() => { cleanup(); sessionStorage.clear(); localStorage.clear(); vi.unstubAllGlobals(); });

async function login() {
  render(<App />);
  fireEvent.change(screen.getByLabelText('ID'), { target: { value: 'admin-land-transport' } });
  fireEvent.change(screen.getByLabelText('비밀번호'), { target: { value: 'AdminLand!2026' } });
  fireEvent.click(screen.getByRole('button', { name: '로그인', exact: true }));
}

describe('처리 서버 연결 오류 안내', () => {
  it.each(['', '<html>Bad Gateway</html>'])('explains an offline backend for empty and non-JSON proxy responses', async (body) => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(body, { status: 502 })));
    await login();
    expect(await screen.findByText(/백엔드 서버가 실행 중인지 확인해 주세요.*HTTP 502/)).toBeTruthy();
  });

  it('explains network failures instead of exposing browser errors', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')));
    await login();
    expect(await screen.findByText(/서버 실행 상태와 처리 서버 주소를 확인해 주세요/)).toBeTruthy();
  });

  it('preserves the actual authentication error', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ detail: '계정 또는 비밀번호가 올바르지 않습니다.' }), { status: 401 })));
    await login();
    expect(await screen.findByText('계정 또는 비밀번호가 올바르지 않습니다.')).toBeTruthy();
  });

  it('preserves detailed service errors instead of calling every 503 a connection failure', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ detail: '현재 요청을 처리할 수 없습니다. 잠시 후 다시 시도해 주세요.' }), { status: 503 })));
    await login();
    expect(await screen.findByText('현재 요청을 처리할 수 없습니다. 잠시 후 다시 시도해 주세요.')).toBeTruthy();
  });
});
