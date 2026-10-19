import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import ComplaintFilters, { appendComplaintFilters, hasComplaintFilters } from '../src/ComplaintFilters.jsx';
import CitizenComplaintTools from '../src/CitizenComplaintTools.jsx';
import AIRecommendations, { useRecommendations } from '../src/AIRecommendations.jsx';

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
const record = { id: 81, title: '조건 조회 테스트', content: '본인 민원 원문', category: '교통·국토', department_id: 2, complaint_status: '접수', latest_response_state: '', created_at: '2026-10-19' };
const apiMock = () => vi.fn(async () => ({ complaints: [record], total: 1 }));
function Harness({ api }) {
  const recommendation = useRecommendations(api);
  return <AIRecommendations recommendation={recommendation} />;
}
function openLookup() {
  fireEvent.click(screen.getByLabelText('대화 옵션'));
  fireEvent.click(screen.getByRole('button', { name: /특정 민원 조회/ }));
}
const partialCases = [
  { name: '부서만', filters: { departments: ['교통·국토'], view_status: '' }, selection: ['checkbox', '교통·국토'], expected: '교통·국토 · 전체 상태' },
  { name: '상태만', filters: { departments: [], view_status: 'completed' }, selection: ['radio', '접수 완료'], expected: '전체 부서 · 접수 완료' },
];

describe('민원 조회 조건 선택 완화', () => {
  it.each(partialCases)('$name 선택만으로 조회 버튼 활성화 및 전달', ({ filters, selection }) => {
    const onApply = vi.fn(); render(<ComplaintFilters onApply={onApply} />);
    const button = screen.getByRole('button', { name: '조회하기' });
    expect(button.disabled).toBe(true);
    fireEvent.click(screen.getByRole(selection[0], { name: selection[1] }));
    expect(button.disabled).toBe(false); fireEvent.click(button);
    expect(onApply).toHaveBeenCalledWith(filters);
  });

  it('전체 상태로 돌아가면 부서는 유지하고 상태 조건만 제거', () => {
    const onApply = vi.fn(); render(<ComplaintFilters onApply={onApply} />);
    fireEvent.click(screen.getByRole('checkbox', { name: '교통·국토' }));
    fireEvent.click(screen.getByRole('radio', { name: '접수 완료' }));
    fireEvent.click(screen.getByRole('radio', { name: '전체 상태' }));
    fireEvent.click(screen.getByRole('button', { name: '조회하기' }));
    expect(onApply).toHaveBeenCalledWith({ departments: ['교통·국토'], view_status: '' });
    fireEvent.click(screen.getByRole('button', { name: '조건 초기화' }));
    expect(screen.getByRole('button', { name: '조회하기' }).disabled).toBe(true);
    expect(screen.getByRole('radio', { name: '전체 상태' }).checked).toBe(true);
  });

  it('선택하지 않은 조건은 URL에 넣지 않고 유효하지 않은 조건만 있으면 적용하지 않음', () => {
    const byDepartment = appendComplaintFilters(new URLSearchParams(), partialCases[0].filters);
    expect(byDepartment.getAll('departments')).toEqual(['교통·국토']);
    expect(byDepartment.has('view_status')).toBe(false);
    const byStatus = appendComplaintFilters(new URLSearchParams(), partialCases[1].filters);
    expect(byStatus.has('departments')).toBe(false);
    expect(byStatus.get('view_status')).toBe('completed');
    expect(hasComplaintFilters({ departments: ['없는 부서'], view_status: 'bad' })).toBe(false);
  });

  it.each(partialCases)('AI 추천 답변의 조회 카드에서 $name 선택 시 실제 도구 요청까지 연결', async ({ filters, selection, expected }) => {
    const api = apiMock(); render(<Harness api={api} />); openLookup();
    const card = screen.getByRole('region', { name: '민원 조회 조건' });
    fireEvent.click(within(card).getByRole(selection[0], { name: selection[1] }));
    const apply = within(card).getByRole('button', { name: '조회하기' });
    expect(apply.disabled).toBe(false); fireEvent.click(apply);
    await screen.findByRole('button', { name: '민원 #81 조건 조회 테스트 확인' });
    expect(screen.getByText('조회 조건: ' + expected)).toBeTruthy();
    const paths = api.mock.calls.map(([path]) => path).filter((path) => path.startsWith('/api/complaints?'));
    expect(paths.length).toBeGreaterThan(0);
    for (const path of paths) {
      const params = new URL(path, 'http://test').searchParams;
      expect(params.getAll('departments')).toEqual(filters.departments);
      expect(params.get('view_status')).toBe(filters.view_status || null);
    }
    expect(api.mock.calls.every(([, options]) => !options?.method)).toBe(true);
  });

  it.each(partialCases)('민원 도구가 외부의 $name 조회 요청을 그대로 적용', async ({ filters, expected }) => {
    const api = apiMock();
    render(<CitizenComplaintTools api={api} request={{ key: 'partial-filter', intent: 'filter', filters }} />);
    await screen.findByRole('button', { name: '민원 #81 조건 조회 테스트 확인' });
    await waitFor(() => expect(api).toHaveBeenCalled());
    expect(screen.getByText('조회 조건: ' + expected)).toBeTruthy();
    expect(screen.queryByText(/undefined/)).toBeNull();
    expect(screen.queryByRole('region', { name: '민원 조회 조건' })).toBeNull();
  });
});
