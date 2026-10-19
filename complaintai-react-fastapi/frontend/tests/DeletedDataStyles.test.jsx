import React from 'react';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { afterEach, describe, expect, it } from 'vitest';
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import DeletedData from '../src/DeletedData.jsx';
import departments from '../../app/departments.json';
// Vitest stubs imported CSS; read the real source to test the actual cascade.
const trashStyles = readFileSync(resolve(process.cwd(), 'src/deletedData.css'), 'utf8');
const sharedStyles = readFileSync(resolve(process.cwd(), 'src/styles.css'), 'utf8');

afterEach(cleanup);

function setup() {
  const api = async (path) => {
    if (path === '/api/trash/counts') return {
      total: 1, unassigned: 0, policy: { capacity: 10000 },
      departments: departments.map((d) => ({ ...d, count: d.id === 2 ? 1 : 0 })),
    };
    return { total: 1, items: [{ id: 1, title: '스타일 확인용 민원', department_id: 2,
      department_name: '교통·국토', deleted_at: '2026-10-19T01:00:00Z', deleted_by_role: 'admin', can_manage: true }] };
  };
  // Match main.jsx: component CSS loads before the application's shared styles.
  render(<><style>{trashStyles + '\n' + sharedStyles}</style><DeletedData api={api} ownDepartmentId={2} /></>);
}

describe('삭제 보관함 버튼 스타일', () => {
  it('shares the category grid and preserves selected and own-department colors', async () => {
    setup(); await screen.findByText('스타일 확인용 민원');
    const group = screen.getByRole('group', { name: '삭제된 데이터 부서 필터' });
    expect(group.classList.contains('grid')).toBe(true);
    const all = within(group).getByRole('button', { name: /전체 부서/ });
    const own = within(group).getByRole('button', { name: /교통·국토/ });
    expect(getComputedStyle(all).borderRadius).toBe('10px');
    expect(getComputedStyle(all).backgroundColor).toBe('rgb(237, 242, 255)');
    expect(getComputedStyle(own).backgroundColor).toBe('rgb(233, 249, 255)');
    expect(own.firstElementChild.tagName).toBe('B');
    expect(own.firstElementChild.textContent).toBe('1');
    fireEvent.click(own);
    expect(own.getAttribute('aria-pressed')).toBe('true');
    expect(getComputedStyle(own).backgroundColor).toBe('rgb(216, 245, 255)');
  });

  it('styles selection, restoration and permanent-delete controls without losing disabled states', async () => {
    setup(); await screen.findByText('스타일 확인용 민원');
    const selection = screen.getByRole('button', { name: '선택 영구 삭제' });
    const restore = screen.getByRole('button', { name: '복원' });
    const permanent = screen.getByRole('button', { name: '영구 삭제' });
    for (const button of [selection, restore, permanent]) {
      expect(getComputedStyle(button).borderRadius).toBe('9px');
      expect(getComputedStyle(button).minHeight).toBe('40px');
    }
    expect(selection.disabled).toBe(true);
    expect(getComputedStyle(selection).opacity).toBe('0.48');
    expect(getComputedStyle(permanent).color).toBe('rgb(180, 50, 50)');
    expect(getComputedStyle(restore).backgroundColor).toBe('rgb(237, 242, 255)');
    fireEvent.click(screen.getByLabelText('스타일 확인용 민원 선택'));
    expect(selection.disabled).toBe(false);
    fireEvent.click(selection);
    const dialog = screen.getByRole('dialog');
    expect(getComputedStyle(within(dialog).getByRole('button', { name: '영구 삭제 실행' })).color).toBe('rgb(180, 50, 50)');
  });
});
