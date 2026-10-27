import React from 'react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { RagImportProgress } from '../src/RagImport.jsx';
import App from '../src/App.jsx';

beforeEach(() => sessionStorage.clear());
afterEach(() => { cleanup(); sessionStorage.clear(); localStorage.clear(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });
const job = (fields={}) => ({id:'unit-job',source_file:'unit.csv',status:'processing',stage:'embedding',total_rows:10,completed_rows:10,saved_rows:10,duplicate_rows:0,failed_rows:0,...fields});
const importer = (value) => ({job:value,uploading:false,cancelling:false,busy:value.status==='processing',cancel:vi.fn(),resume:vi.fn()});

it('does not offer completion dismissal before successful completion', () => {
  const view=render(<RagImportProgress importer={importer(job())}/>);
  expect(screen.getByRole('progressbar').getAttribute('aria-valuenow')).toBe('99');
  expect(screen.queryByRole('button',{name:'RAG 파일 처리 완료 표시 닫기'})).toBeNull();
  for (const status of ['failed','cancelled']) {
    view.rerender(<RagImportProgress importer={importer(job({status,stage:status,last_error:'시험 오류'}))}/>);
    expect(screen.queryByRole('button',{name:'RAG 파일 처리 완료 표시 닫기'})).toBeNull();
  }
});

it('dismisses only the completed RAG indicator and persists that dismissal in the session', () => {
  const value=job({status:'completed',stage:'completed',finished_at:'2026-10-26T00:00:00Z'});
  const view=render(<><div role="progressbar" aria-label="민원 파일 처리"/><RagImportProgress importer={importer(value)}/></>);
  expect(screen.getByRole('progressbar',{name:'RAG 파일 처리'}).getAttribute('aria-valuenow')).toBe('100');
  fireEvent.click(screen.getByRole('button',{name:'RAG 파일 처리 완료 표시 닫기'}));
  expect(screen.queryByRole('progressbar',{name:'RAG 파일 처리'})).toBeNull();
  expect(screen.getByRole('progressbar',{name:'민원 파일 처리'})).toBeTruthy();
  view.unmount();
  render(<RagImportProgress importer={importer(value)}/>);
  expect(screen.queryByRole('progressbar',{name:'RAG 파일 처리'})).toBeNull();
});

it('shows progress again for a new job or a resumed running job', async () => {
  const value=job({status:'completed',stage:'completed'});
  const view=render(<RagImportProgress importer={importer(value)}/>);
  fireEvent.click(screen.getByRole('button',{name:'RAG 파일 처리 완료 표시 닫기'}));
  view.rerender(<RagImportProgress importer={importer(job({id:'new-job',completed_rows:3,saved_rows:3}))}/>);
  await waitFor(()=>expect(screen.getByRole('progressbar').getAttribute('aria-valuenow')).toBe('30'));
  view.rerender(<RagImportProgress importer={importer(job({id:'unit-job',stage:'chunking',completed_rows:5,saved_rows:5}))}/>);
  expect(screen.getByRole('progressbar').getAttribute('aria-valuenow')).toBe('50');
});

it('dismisses completed complaint and RAG indicators independently in the actual App', async () => {
  const user={role:'admin',name:'시험 관리자',department_id:2,department_name:'교통·국토'};
  sessionStorage.setItem('complaintai.fastapi.auth',JSON.stringify({token:'unit-only',user}));
  let status='processing';
  const response=value=>new Response(JSON.stringify(value),{status:200});
  vi.stubGlobal('fetch',vi.fn(async (url,options={})=>{
    const path=new URL(url).pathname;
    if(path==='/api/auth/context')return response({user});
    if(path==='/api/departments')return response({departments:[{id:2,name:'교통·국토'}]});
    if(path==='/api/complaints/counts')return response({categories:[]});
    if(path==='/api/complaints')return response({complaints:[],total:0});
    if(path==='/api/rag-imports/latest')return response({job:job({status:'completed',stage:'completed'})});
    if(path==='/api/imports'&&options.method==='POST')return response({job_id:'complaint-unit',status:'queued',needs_mapping:false,total_rows:10});
    if(path==='/api/imports/complaint-unit')return response({id:'complaint-unit',status,total_rows:10,completed_rows:10,saved_rows:10});
    throw new Error(`Unexpected unit API ${path}`);
  }));
  const intervals=vi.spyOn(window,'setInterval');
  render(<App/>);
  fireEvent.click(screen.getByRole('button',{name:'파일 자료 관리',exact:true}));
  await screen.findByRole('button',{name:'RAG 파일 처리 완료 표시 닫기'});
  const start=()=>{
    fireEvent.change(screen.getByLabelText(/^민원 파일 선택/),{target:{files:[new File(['제목,본문\n제목,내용'],'unit.csv')]}});
    fireEvent.click(screen.getByRole('button',{name:'외부 민원 저장 시작'}));
  };
  start();
  await waitFor(()=>expect(intervals.mock.calls.some(call=>call[1]===1000)).toBe(true));
  const poll=intervals.mock.calls.filter(call=>call[1]===1000).at(-1)[0];
  await act(async()=>{await poll();});
  expect(screen.getByRole('progressbar',{name:'민원 파일 처리'}).getAttribute('aria-valuenow')).toBe('99');
  expect(screen.queryByRole('button',{name:'민원 파일 처리 완료 표시 닫기'})).toBeNull();
  status='completed';
  await act(async()=>{await poll();});
  fireEvent.click(await screen.findByRole('button',{name:'민원 파일 처리 완료 표시 닫기'}));
  expect(screen.queryByRole('progressbar',{name:'민원 파일 처리'})).toBeNull();
  expect(screen.getByRole('progressbar',{name:'RAG 파일 처리'})).toBeTruthy();
  fireEvent.click(screen.getByRole('button',{name:'RAG 파일 처리 완료 표시 닫기'}));
  expect(screen.queryByRole('progressbar')).toBeNull();
  status='processing';
  fireEvent.click(screen.getByRole('button',{name:'파일 자료 관리',exact:true}));
  start();
  await screen.findByRole('progressbar',{name:'민원 파일 처리'});
});
