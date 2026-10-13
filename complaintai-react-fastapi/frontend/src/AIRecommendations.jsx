import { useCallback, useEffect, useRef, useState } from 'react';
import CitizenComplaintTools from './CitizenComplaintTools.jsx';

const stageLabels = {
  routing: '요청하신 답변이나 기능을 확인하고 있어요.',
  queued: '답변 순서를 기다리고 있어요.',
  embedding: '말씀하신 내용을 살펴보고 있어요.',
  retrieval: '관련 공식 자료를 찾고 있어요.',
  reranking: '질문에 맞는 자료를 확인하고 있어요.',
  generation: '추천 답변을 작성하고 있어요.',
};

export function useRecommendations(api) {
  const [messages, setMessages] = useState([]);
  const [draft, setDraft] = useState('');
  const [job, setJob] = useState(null);
  const [error, setError] = useState('');
  const [creating, setCreating] = useState(false);
  const [toolsOpen, setToolsOpen] = useState(false);
  const [toolRequest, setToolRequest] = useState(null);
  const [toolContext, updateToolContext] = useState(null);
  const toolSequence = useRef(0);
  const openTool = useCallback((request = 'list') => {
    const next = typeof request === 'string' ? { intent: request } : request;
    if (!['create', 'list', 'edit', 'delete'].includes(next.intent)) return;
    updateToolContext(null);
    setToolRequest({ ...next, key: ++toolSequence.current });
    setToolsOpen(true);
  }, []);
  const closeTools = useCallback(() => setToolsOpen(false), []);
  const toggleTools = () => {
    if (toolsOpen) setToolsOpen(false);
    else if (toolRequest) setToolsOpen(true);
    else openTool('list');
  };
  const reopenTool = (request) => {
    if (toolRequest?.intent === request.intent && toolContext?.mode === request.intent &&
      (!request.complaint_id || toolContext.complaint_id === request.complaint_id)) setToolsOpen(true);
    else openTool(request);
  };
  const setToolContext = useCallback((next) => updateToolContext((previous) =>
    JSON.stringify(previous) === JSON.stringify(next) ? previous : next), []);
  const notifyToolChanged = useCallback((event) => {
    setMessages((previous) => [...previous, { id: `receipt-${Date.now()}-${++toolSequence.current}`,
      role: 'assistant', kind: 'notice', content: event.message || '민원 도구의 처리 결과를 확인해 주세요.' }]);
  }, []);
  const requestLock = useRef(false);
  const generation = useRef(0);
  const activeTurn = useRef(null);
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; generation.current++; }; }, []);
  const busy = creating || ['queued', 'processing'].includes(job?.status);
  const updateTurn = useCallback((id, next) => {
    setMessages((previous) => previous.map((message) => {
      if (message.id === id) return { ...message, job: next };
      if (message.id === `${id}-user`) return { ...message, title: next.title || message.title,
        content: message.preserveContent ? message.content : next.content || message.content };
      return message;
    }));
  }, []);

  const start = useCallback(async (body, preview = {}, retryId = null, routing = null) => {
    if (requestLock.current) return false;
    requestLock.current = true;
    const sequence = ++generation.current;
    const id = retryId || `turn-${Date.now()}-${sequence}`;
    activeTurn.current = id;
    const routeInput = routing && (typeof routing === 'object' ? routing : {
      message: preview.content || body.content,
      tool_context: toolsOpen && toolContext ? {
        mode: ['create', 'list', 'edit', 'delete'].includes(toolContext.mode) ? toolContext.mode : 'list',
        complaint_id: Number.isInteger(toolContext.complaint_id) ? toolContext.complaint_id : null,
        title: (toolContext.title || '').slice(0, 200), content: (toolContext.content || '').slice(0, 4000),
      } : null,
      recent_messages: messages.flatMap((message) => {
        const text = message.role === 'user' ? message.content : message.content || message.job?.result?.answer;
        return text ? [{ role: message.role, content: text.slice(0, 600) }] : [];
      }).slice(-6),
    });
    setCreating(true); setError(''); setJob(null);
    if (retryId) {
      setMessages((previous) => previous.map((message) => message.id === id ? { ...message,
        job: routing ? { status: 'routing', stage: 'routing' } : null } : message));
    } else {
      setMessages((previous) => [...previous,
        { id: `${id}-user`, role: 'user', title: preview.title || body.title || '', content: preview.content || body.content || '', showTitle: preview.showTitle ?? !!body.complaint_id,
          preserveContent: !!routing || preview.preserveContent },
        { id, role: 'assistant', job: routing ? { status: 'routing', stage: 'routing' } : null, request: body, routing: routeInput },
      ]);
    }
    try {
      if (routeInput) {
        const decision = await api('/api/ai-recommendations/intent', { method: 'POST',
          headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(routeInput) });
        if (!mounted.current || sequence !== generation.current) return false;
        if (!['answer', 'create', 'list', 'edit', 'delete', 'close_tools'].includes(decision.intent)) {
          throw new Error('요청한 기능을 확인하지 못했습니다. 다시 질문하거나 민원 도구 버튼을 이용해 주세요.');
        }
        if (decision.intent !== 'answer') {
          if (decision.intent === 'close_tools') closeTools();
          else if (['create', 'edit', 'delete'].includes(decision.intent) && toolContext?.mode === decision.intent &&
            (!decision.complaint_id || decision.complaint_id === toolContext.complaint_id) &&
            (decision.intent !== 'create' || !decision.draft)) setToolsOpen(true);
          else openTool({ intent: decision.intent, complaint_id: decision.complaint_id, draft: decision.draft });
          setMessages((previous) => previous.map((message) => message.id === id
            ? { ...message, kind: 'tool', content: decision.message, toolAction: decision, job: null } : message));
          requestLock.current = false;
          return true;
        }
        const context = routeInput.tool_context;
        if (context?.content?.trim()) {
          body = { title: (context.title || '민원 도구에서 상담').slice(0, 200),
            content: `민원인이 작성하거나 선택한 내용:\n${context.content}\n\n현재 질문:\n${routeInput.message}` };
        }
        setMessages((previous) => previous.map((message) => message.id === id ? { ...message, job: null } : message));
      }
      const next = await api('/api/ai-recommendations', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
      if (mounted.current && sequence === generation.current) {
        setJob(next); updateTurn(id, next);
        if (['completed', 'failed'].includes(next.status)) requestLock.current = false;
      }
      return true;
    } catch (failure) {
      if (mounted.current && sequence === generation.current) {
        const failed = { status: 'failed', error_message: failure.message };
        setJob(failed); updateTurn(id, failed); requestLock.current = false;
      }
      return false;
    } finally {
      if (mounted.current && sequence === generation.current) setCreating(false);
    }
  }, [api, updateTurn, messages, toolsOpen, toolContext, toolRequest, openTool, closeTools]);

  useEffect(() => {
    if (!job || !['queued', 'processing'].includes(job.status)) return;
    let stopped = false;
    let timer;
    const id = activeTurn.current;
    const poll = async () => {
      try {
        const next = await api(`/api/ai-recommendations/${job.id}`);
        if (stopped) return;
        setError(''); setJob(next); updateTurn(id, next);
        if (['completed', 'failed'].includes(next.status)) requestLock.current = false;
        else timer = window.setTimeout(poll, 1500);
      } catch (failure) {
        if (stopped) return;
        setError(`답변 상태를 확인하지 못했습니다. 연결되면 다시 확인할게요. ${failure.message}`);
        timer = window.setTimeout(poll, 4000);
      }
    };
    timer = window.setTimeout(poll, 500);
    return () => { stopped = true; window.clearTimeout(timer); };
  }, [api, job?.id, job?.status, updateTurn]);

  const send = () => {
    const content = draft.trim();
    if (!content || requestLock.current) return;
    const parent = [...messages].reverse().find((message) => message.role === 'assistant' && message.job?.status === 'completed');
    const body = parent
      ? { parent_recommendation_id: parent.job.id, content }
      : { title: content.split('\n')[0].slice(0, 80), content };
    setDraft('');
    return start(body, { content, preserveContent: true }, null, true);
  };
  const reset = () => {
    if (requestLock.current) return;
    generation.current++; activeTurn.current = null;
    setMessages([]); setDraft(''); setJob(null); setError('');
  };
  return { api, messages, draft, setDraft, job, error, busy, creating, start, send, reset,
    toolsOpen, toolRequest, toolContext, openTool, closeTools, toggleTools, reopenTool, setToolContext, notifyToolChanged };
}

function MessageText({ text }) {
  // Render the limited bold formatting used by model answers as React text, never HTML.
  return <p className="ai-message-text">{text.split(/(\*\*[^*\n]+\*\*)/g).map((part, index) =>
    part.startsWith('**') && part.endsWith('**') ? <strong key={index}>{part.slice(2, -2)}</strong> : part)}</p>;
}

function AnswerOptions({ result }) {
  if (!result.checks?.length && !result.next_steps?.length && !result.citations?.length) return null;
  return <details className="ai-answer-options">
    <summary>답변 옵션{result.citations?.length > 0 && <span>출처 {result.citations.length}개</span>}</summary>
    <div className="ai-answer-extras">
      {result.checks?.length > 0 && <div><h3>확인할 사항</h3><ul>{result.checks.map((item, index) => <li key={index}>{item}</li>)}</ul></div>}
      {result.next_steps?.length > 0 && <div><h3>다음 절차</h3><ol>{result.next_steps.map((item, index) => <li key={index}>{item}</li>)}</ol></div>}
      {result.citations?.length > 0 && <div className="ai-citations"><h3>답변에 사용한 공식 자료</h3>{result.citations.map((citation) => <details key={citation.number}>
        <summary>[{citation.number}] {citation.title} · {citation.locator}</summary>
        <p className="muted">{citation.publisher} · {citation.kind} · {citation.version}</p>
        <p className="ai-message-text">{citation.excerpt}</p>
        <a href={citation.url} target="_blank" rel="noopener noreferrer">공식 원문 보기 ↗</a>
        <small>자료 확인: {new Date(citation.retrieved_at).toLocaleDateString('ko-KR')}</small>
      </details>)}</div>}
    </div>
  </details>;
}

function AssistantMessage({ message, recommendation }) {
  const job = message.job;
  const result = job?.result;
  const isToolMessage = ['tool', 'notice'].includes(message.kind);
  const pending = !isToolMessage && (!job || ['routing', 'queued', 'processing'].includes(job.status));
  return <article className="ai-chat-message ai-chat-message-assistant" aria-label="AI 답변">
    <span className="ai-chat-avatar" aria-hidden="true">AI</span>
    <div className="ai-chat-message-body">
      <span className="ai-chat-sender">민원 상담 도우미</span>
      <div className={`ai-chat-bubble ${result?.answer_status === 'insufficient_evidence' ? 'ai-chat-insufficient' : ''}`}>
        {isToolMessage && <><MessageText text={message.content} />{message.toolAction?.intent !== 'close_tools' && message.kind === 'tool' &&
          <button className="ai-chat-retry" onClick={() => recommendation.reopenTool(message.toolAction)}>도구 다시 열기</button>}</>}
        {pending && <div className="ai-chat-thinking" role="status"><span className="ai-spinner" aria-hidden="true" /><span>{stageLabels[job?.stage] || '답변을 준비하고 있어요.'}</span></div>}
        {job?.status === 'failed' && <><p className="ai-message-text" role="alert">{job.error_message || '답변을 생성하지 못했습니다. 다시 시도해 주세요.'}</p><button className="ai-chat-retry" disabled={recommendation.busy} onClick={() => recommendation.start(message.request, {}, message.id, message.routing)}>다시 시도</button></>}
        {result && <>
          {result.answer_status === 'insufficient_evidence' && <span className="ai-chat-evidence-note">추가 근거가 필요해요</span>}
          <MessageText text={result.answer} />
          <AnswerOptions result={result} />
        </>}
      </div>
      {pending && recommendation.error && <p className="ai-chat-connection" role="alert">{recommendation.error}</p>}
    </div>
  </article>;
}

export default function AIRecommendations({ recommendation, api = recommendation.api, onToolsChanged }) {
  const { messages, draft, setDraft, busy, send, start, reset } = recommendation;
  const { toolsOpen = false, toolRequest, openTool, closeTools, toggleTools } = recommendation;
  const [formOpen, setFormOpen] = useState(false);
  const [title, setTitle] = useState('');
  const [content, setContent] = useState('');
  const optionsRef = useRef(null);
  const scrollRef = useRef(null);
  const inputRef = useRef(null);
  const nearBottom = useRef(true);
  const previousCount = useRef(0);
  const composing = useRef(false);

  useEffect(() => {
    const element = scrollRef.current;
    if (element && (nearBottom.current || previousCount.current !== messages.length)) {
      element.scrollTop = element.scrollHeight;
      nearBottom.current = true;
    }
    previousCount.current = messages.length;
  }, [messages, recommendation.error, formOpen]);
  useEffect(() => {
    if (inputRef.current) {
      inputRef.current.style.height = 'auto';
      inputRef.current.style.height = `${Math.min(140, Math.max(48, inputRef.current.scrollHeight))}px`;
    }
  }, [draft, formOpen]);
  useEffect(() => {
    const closeOutside = (event) => { if (optionsRef.current && !optionsRef.current.contains(event.target)) optionsRef.current.open = false; };
    document.addEventListener('pointerdown', closeOutside);
    return () => document.removeEventListener('pointerdown', closeOutside);
  }, []);
  const closeOptions = () => { if (optionsRef.current) optionsRef.current.open = false; };
  const newConversation = () => { closeOptions(); setFormOpen(false); reset(); inputRef.current?.focus(); };
  const submitForm = (event) => {
    event.preventDefault();
    if (busy || !title.trim() || !content.trim()) return;
    reset(); setFormOpen(false);
    start({ title: title.trim(), content: content.trim() }, { showTitle: true });
  };

  return <div className={`ai-chat-workbench ${toolsOpen ? 'ai-chat-workbench-tools-open' : ''}`}><section className="panel ai-chat" aria-label="AI 추천 답변" onKeyDown={(event) => {
    if (event.key === 'Escape') { closeOptions(); setFormOpen(false); }
  }}>
    <div className="ai-chat-header">
      <div className="ai-chat-identity"><span className="ai-chat-avatar" aria-hidden="true">AI</span><div><h2>민원 상담 도우미</h2><p><span className="ai-chat-status-dot" />공식 자료를 바탕으로 안내해 드려요</p></div></div>
      <div className="ai-chat-header-actions"><button className={`ai-tools-toggle ${toolsOpen ? 'active' : ''}`} onClick={toggleTools} aria-expanded={toolsOpen} aria-controls="ai-citizen-tool-window">{toolsOpen ? '민원 도구 닫기' : '민원 도구 열기'}</button><details className="ai-chat-options" ref={optionsRef}>
        <summary aria-label="대화 옵션"><span aria-hidden="true">⋯</span> 옵션</summary>
        <div className="ai-chat-options-menu">
          <button disabled={busy} onClick={newConversation}>새 대화 시작</button>
          <button onClick={() => { closeOptions(); openTool('create'); }}>새로운 민원 작성하기<small>오른쪽 도구에서 작성하고 접수</small></button>
          <button onClick={() => { closeOptions(); openTool('list'); }}>작성된 민원에서 선택하기<small>오른쪽 도구에서 확인·수정·삭제</small></button>
          <button disabled={busy} onClick={() => { closeOptions(); setTitle(''); setContent(draft); setFormOpen(true); }}>제목·내용으로 AI 답변 받기<small>민원을 접수하지 않고 예상 답변 확인</small></button>
          <details className="ai-chat-usage"><summary>이용 안내</summary><p>대화로 민원 작성이나 확인을 요청하면 오른쪽 도구를 열어 드립니다. 실제 접수·수정·삭제는 도구 창에서 버튼으로 확정합니다.</p><p>다른 메뉴로 이동해도 대화는 유지됩니다. 새로고침하거나 로그아웃하면 화면의 대화는 초기화됩니다.</p></details>
        </div>
      </details></div>
    </div>

    <div className="ai-chat-history" ref={scrollRef} role="log" aria-label="민원 상담 대화" aria-live="polite" aria-relevant="additions text" onScroll={(event) => {
      const element = event.currentTarget;
      nearBottom.current = element.scrollHeight - element.scrollTop - element.clientHeight < 100;
    }}>
      <div className="ai-chat-date">오늘</div>
      <article className="ai-chat-message ai-chat-message-assistant">
        <span className="ai-chat-avatar" aria-hidden="true">AI</span>
        <div className="ai-chat-message-body"><span className="ai-chat-sender">민원 상담 도우미</span><div className="ai-chat-bubble"><p className="ai-message-text">안녕하세요! 어떤 도움이 필요하신가요?{ '\n' }궁금한 점을 물어보거나 “민원을 작성하고 싶어”, “내 민원을 확인하고 싶어”라고 말씀해 주세요.</p></div></div>
      </article>
      {messages.map((message) => message.role === 'user'
        ? <article key={message.id} className="ai-chat-message ai-chat-message-user" aria-label="내 메시지"><div className="ai-chat-message-body"><span className="ai-chat-sender">나</span><div className="ai-chat-bubble">{message.showTitle && message.title && <strong className="ai-chat-topic">{message.title}</strong>}<p className="ai-message-text">{message.content || '선택한 민원을 불러오고 있어요.'}</p></div></div></article>
        : <AssistantMessage key={message.id} message={message} recommendation={recommendation} />)}
    </div>

    <div className="ai-chat-bottom">
      {formOpen ? <form className="ai-chat-structured" aria-label="새로운 민원 작성하기" onSubmit={submitForm}>
        <div className="ai-chat-structured-heading"><h3>새로운 민원으로 대화하기</h3><button type="button" className="ai-chat-close" aria-label="작성 취소" onClick={() => setFormOpen(false)}>×</button></div>
        <label htmlFor="ai-complaint-title">민원 제목<input id="ai-complaint-title" required maxLength={200} value={title} onChange={(event) => setTitle(event.target.value)} placeholder="민원 제목을 입력해 주세요" /></label>
        <label htmlFor="ai-complaint-content">민원 내용<textarea id="ai-complaint-content" required maxLength={12000} value={content} onChange={(event) => setContent(event.target.value)} placeholder="상황과 궁금한 점을 적어 주세요" /></label>
        <button className="primary" type="submit" disabled={busy || !title.trim() || !content.trim()}>대화 시작</button>
      </form> : <form className="ai-chat-compose" aria-label="메시지 보내기" onSubmit={(event) => { event.preventDefault(); if (!busy) send(); }}>
        <label className="ai-chat-input-label" htmlFor="ai-chat-input">메시지</label>
        <textarea id="ai-chat-input" ref={inputRef} rows={1} maxLength={4000} value={draft} disabled={busy} onChange={(event) => setDraft(event.target.value)} placeholder={busy ? '답변을 기다리고 있어요…' : messages.length ? '이어서 궁금한 점을 물어보세요' : '상황이나 궁금한 점을 입력해 주세요'} onCompositionStart={() => { composing.current = true; }} onCompositionEnd={() => { composing.current = false; }} onKeyDown={(event) => {
          if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing && !composing.current && event.keyCode !== 229) {
            event.preventDefault(); if (!busy) send();
          }
        }} />
        <button className="ai-chat-send" type="submit" disabled={busy || !draft.trim()}><span aria-hidden="true">↑</span><span>전송</span></button>
      </form>}
      <p className="ai-chat-disclaimer">AI 안내는 참고용이며 담당 기관의 공식 답변과 다를 수 있습니다.</p>
    </div>
  </section>{toolRequest && <div id="ai-citizen-tool-window" className="ai-chat-tool-slot" hidden={!toolsOpen}>
    <CitizenComplaintTools api={api} request={toolRequest} visible={toolsOpen} onContextChange={recommendation.setToolContext}
      onClose={closeTools} recommendationBusy={busy} onChanged={(event) => { recommendation.notifyToolChanged(event); onToolsChanged?.(event); }}
      onRecommend={(record) => record.id
        ? start({ complaint_id: record.id }, { title: record.title, content: record.content, showTitle: true })
        : start({ title: record.title || '작성 중인 민원', content: record.content }, { showTitle: true })} />
  </div>}</div>;
}
