import { useCallback, useEffect, useRef, useState } from 'react';
import CitizenComplaintTools from './CitizenComplaintTools.jsx';
import ComplaintFilters from './ComplaintFilters.jsx';

const stageLabels = {
  routing: '요청하신 답변이나 기능을 확인하고 있어요.',
  queued: '답변 순서를 기다리고 있어요.',
  embedding: '말씀하신 내용을 살펴보고 있어요.',
  retrieval: '관련 공식 자료를 찾고 있어요.',
  reranking: '질문에 맞는 자료를 확인하고 있어요.',
  generation: '추천 답변을 작성하고 있어요.',
};

// Allow the backend's 20-minute deadline to return its own error first.
export const CHAT_REQUEST_TIMEOUT_MS = 1210000;

export async function requestChat(api, body, controller, timeoutMs = CHAT_REQUEST_TIMEOUT_MS) {
  const { signal } = controller;
  let onAbort;
  const aborted = new Promise((_, reject) => {
    onAbort = () => reject(new Error(signal.reason === 'timeout'
      ? '답변 대기 시간이 초과되었습니다. 다시 시도해 주세요.' : '답변 요청을 취소했습니다.'));
    if (signal.aborted) onAbort();
    else signal.addEventListener('abort', onAbort, { once: true });
  });
  const timer = window.setTimeout(() => controller.abort('timeout'), timeoutMs);
  try {
    return await Promise.race([api('/api/chat', { method: 'POST', signal,
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }), aborted]);
  } finally {
    window.clearTimeout(timer); signal.removeEventListener('abort', onAbort);
  }
}

export function useRecommendations(api) {
  const [messages, setMessages] = useState([]);
  const [draft, setDraft] = useState('');
  const [job, setJob] = useState(null);
  const [error, setError] = useState('');
  const [creating, setCreating] = useState(false);
  const [toolsOpen, setToolsOpen] = useState(false);
  const [toolRequest, setToolRequest] = useState(null);
  const [toolContext, updateToolContext] = useState(null);
  const [lookupPrompt, setLookupPrompt] = useState(null);
  
  // agent_mode 상태 추가 (0: 민원처리모드, 1: 대화모드)
  const [agentMode, setAgentMode] = useState(0);

  const lookupRef = useRef(null);
  const toolSequence = useRef(0);
  
  const openTool = useCallback((request = 'list') => {
    const next = typeof request === 'string' ? { intent: request } : request;
    if (!['create', 'list', 'edit', 'delete', 'filter', 'search'].includes(next.intent)) return;
    lookupRef.current = null; setLookupPrompt(null);
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
  const activeController = useRef(null);

  useEffect(() => { mounted.current = true; return () => {
    mounted.current = false; generation.current++; activeController.current?.abort('unmount');
  }; }, []);
  const busy = creating || ['queued', 'processing'].includes(job?.status);

  const updateTurn = useCallback((id, next) => {
    setMessages((previous) => previous.map((message) => {
      if (message.id === id) return { ...message, job: next };
      if (message.id === `${id}-user`) return { ...message, title: next.title || message.title,
        content: message.preserveContent ? message.content : next.content || message.content };
      return message;
    }));
  }, []);

  const promptLookup = useCallback((intent, existingId = null) => {
    if (requestLock.current && !existingId) return;
    const id = existingId || `lookup-${Date.now()}-${++toolSequence.current}`;
    const prompt = { id, kind: intent };
    lookupRef.current = prompt; setLookupPrompt(prompt);
    const message = { id, role: 'assistant', kind: intent === 'filter' ? 'filter_prompt' : 'search_prompt',
      content: intent === 'filter' ? '어떤 민원들을 조회하시겠습니까?'
        : '어떤 민원을 검색하시겠습니까?\n찾고 싶은 민원의 상황이나 내용을 다음 메시지에 1,000자 이내로 적어 주세요. 제목이 정확하지 않아도 됩니다. 다음 메시지 한 번만 본인이 작성한 민원에서 검색하며, 그 뒤에는 일반 상담으로 돌아갑니다.', job: null };
    setMessages((previous) => existingId ? previous.map((item) => item.id === id ? { ...item, ...message } : item) : [...previous, message]);
  }, []);

  const cancelLookup = useCallback(() => { lookupRef.current = null; setLookupPrompt(null); }, []);
  const applyLookupFilters = useCallback((filters, promptId) => {
    if (requestLock.current || lookupRef.current?.id !== promptId || lookupRef.current?.kind !== 'filter') return;
    if (!filters?.departments?.length || !filters.view_status) return;
    openTool({ intent: 'filter', filters });
    setMessages((previous) => previous.map((item) => item.id === promptId ? { ...item, filters } : item));
  }, [openTool]);

  const searchComplaints = useCallback(async (query, retryId = null) => {
    if (!query.trim() || requestLock.current) return false;
    requestLock.current = true;
    cancelLookup();
    const sequence = ++generation.current;
    const id = retryId || `search-${Date.now()}-${sequence}`;
    const key = ++toolSequence.current;
    const toolAction = { intent: 'search', query, state: 'loading', key };
    activeTurn.current = id;
    updateToolContext(null); setToolRequest(toolAction); setToolsOpen(true);
    setCreating(true); setJob(null); setError('');
    const pendingMessage = { id, role: 'assistant', kind: 'search_result', searchQuery: query,
      content: '작성한 민원에서 검색하고 있어요.', job: { status: 'searching' }, toolAction };
    setMessages((previous) => retryId ? previous.map((item) => item.id === id ? pendingMessage : item) : [...previous,
      { id: `${id}-user`, role: 'user', content: query, preserveContent: true }, pendingMessage]);
    try {
      const results = await api('/api/ai-recommendations/complaint-search', { method: 'POST',
        headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ query }) });
      if (!mounted.current || sequence !== generation.current) return false;
      if (!Array.isArray(results.complaints) || !Array.isArray(results.similar_complaints)) throw new Error('검색 결과를 읽지 못했습니다. 다시 시도해 주세요.');
      const completed = { ...toolAction, state: 'complete', results };
      setToolRequest((previous) => previous?.key === key ? completed : previous);
      setMessages((previous) => previous.map((item) => item.id === id ? { ...item, job: null, toolAction: completed,
        content: `검색된 민원 ${results.complaints.length}건과 유사한 민원 ${results.similar_complaints.length}건을 찾았습니다. 오른쪽 도구에서 확인해 주세요.` } : item));
      return true;
    } catch (failure) {
      if (mounted.current && sequence === generation.current) {
        setToolRequest((previous) => previous?.key === key ? { ...toolAction, state: 'failed', error: failure.message } : previous);
        setMessages((previous) => previous.map((item) => item.id === id ? { ...item, job: { status: 'failed', error_message: failure.message } } : item));
      }
      return false;
    } finally {
      if (mounted.current && sequence === generation.current) { setCreating(false); requestLock.current = false; }
    }
  }, [api, cancelLookup]);

  const start = useCallback(async (body, preview = {}, retryId = null, routing = null) => {
    if (requestLock.current) return false;
    cancelLookup();
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
      setMessages((previous) => previous.map((message) => message.id === id ? {
        ...message, job: routing ? { status: 'routing', stage: 'routing' } : null 
      } : message));
    } else {
      setMessages((previous) => [
        ...previous,
        { id: `${id}-user`, role: 'user', title: preview.title || body.title || '', content: preview.content || body.content || '', showTitle: preview.showTitle ?? !!body.complaint_id, preserveContent: !!routing || preview.preserveContent },
        { id, role: 'assistant', job: routing ? { status: 'routing', stage: 'routing' } : null, request: body, routing: routeInput },
      ]);
    }

    try {
      if (routeInput) {
        const chatBody = { 
          content: routeInput.message, 
          agent_mode: agentMode 
        };

        const controller = new AbortController();
        activeController.current = controller;
        const decision = await requestChat(api, chatBody, controller);

        if (!mounted.current || sequence !== generation.current) return false;

        const responseCode = decision.code;
        const responseMessage = decision.message;
        const resultList = decision.result || [];

        // code === 1 (민원 작성 도구): 추출된 title과 content를 우측 툴 창(create 모드)으로 전달하여 자동 채우기 수행
        if (responseCode === 1) {
          if (resultList.length > 0) {
            const target = resultList[0];
            openTool({ intent: 'create', title: target.title, content: target.content });
          } else {
            openTool('create');
          }
        } else if (responseCode === 2) {
          // code === 2일 때 백엔드가 제공한 result 목록(벡터 정렬된 데이터)을 intent 'list'와 함께 전달
          openTool({ intent: 'list', listKind: 'chat_result', complaints: resultList });
        }

        setMessages((previous) => previous.map((message) => message.id === id ? { 
          ...message, 
          kind: 'tool', 
          content: responseMessage, 
          toolAction: decision, 
          job: null 
        } : message));
        
        requestLock.current = false;
        setCreating(false);
        return true;
      }

      const next = await api('/api/ai-recommendations', { 
        method: 'POST', 
        headers: { 'Content-Type': 'application/json' }, 
        body: JSON.stringify(body) 
      });
      
      if (mounted.current && sequence === generation.current) {
        setJob(next); 
        updateTurn(id, next);
        if (['completed', 'failed'].includes(next.status)) requestLock.current = false;
      }
      return true;
    } catch (failure) {
      if (mounted.current && sequence === generation.current) {
        const failed = { status: 'failed', error_message: failure.message };
        setJob(failed); 
        updateTurn(id, failed); 
        requestLock.current = false;
      }
      return false;
    } finally {
      if (mounted.current && sequence === generation.current) {
        activeController.current = null; setCreating(false);
      }
    }
  }, [api, updateTurn, messages, toolsOpen, toolContext, openTool, cancelLookup, agentMode]);

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
    if (lookupRef.current?.kind === 'search') { setDraft(''); return searchComplaints(content); }
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
    cancelLookup();
    setMessages([]); setDraft(''); setJob(null); setError('');
  };

  const cancelAnswer = () => activeController.current?.abort('user');
  return { api, messages, draft, setDraft, job, error, busy, creating, start, send, reset,
    canCancel: creating && !!activeController.current, cancelAnswer,
    lookupPrompt, promptLookup, cancelLookup, applyLookupFilters, searchComplaints,
    toolsOpen, toolRequest, toolContext, openTool, closeTools, toggleTools, reopenTool, setToolContext, notifyToolChanged,
    agentMode, setAgentMode };
}

function MessageText({ text }) {
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
  const isSearch = message.kind === 'search_result';
  const isLookup = ['filter_prompt', 'search_prompt'].includes(message.kind);
  const isToolMessage = ['tool', 'notice'].includes(message.kind) || isLookup || isSearch;
  const pending = !isToolMessage && (!job || ['routing', 'queued', 'processing'].includes(job.status));
  const activeLookup = recommendation.lookupPrompt?.id === message.id;
  
  return <article className="ai-chat-message ai-chat-message-assistant" aria-label="AI 답변">
    <span className="ai-chat-avatar" aria-hidden="true">AI</span>
    <div className="ai-chat-message-body">
      <span className="ai-chat-sender">민원 상담 도우미</span>
      <div className={`ai-chat-bubble ${result?.answer_status === 'insufficient_evidence' ? 'ai-chat-insufficient' : ''}`}>
        {isToolMessage && job?.status !== 'failed' && <><MessageText text={message.content} /></>}
        {message.kind === 'filter_prompt' && <ComplaintFilters compact initialValue={message.filters} busy={recommendation.busy || !activeLookup}
          onApply={(filters) => recommendation.applyLookupFilters(filters, message.id)} />}
        {message.kind === 'search_prompt' && activeLookup && <button className="ai-chat-retry" onClick={recommendation.cancelLookup}>검색 취소</button>}
        {message.kind === 'search_prompt' && !activeLookup && <p className="ai-chat-connection">이 검색 안내는 종료되었습니다. 다음 메시지는 일반 상담으로 처리합니다.</p>}
        {isSearch && job?.status === 'searching' && <div className="ai-chat-thinking" role="status"><span className="ai-spinner" aria-hidden="true" /><span>민원을 검색하고 있어요.</span></div>}
        {pending && <div className="ai-chat-thinking" role="status"><span className="ai-spinner" aria-hidden="true" /><span>{stageLabels[job?.stage] || '답변을 준비하고 있어요.'}</span></div>}
        {job?.status === 'failed' && <><p className="ai-message-text" role="alert">{job.error_message || '답변을 생성하지 못했습니다. 다시 시도해 주세요.'}</p><button className="ai-chat-retry" disabled={recommendation.busy} onClick={() => isSearch ? recommendation.searchComplaints(message.searchQuery, message.id) : recommendation.start(message.request, {}, message.id, message.routing)}>다시 시도</button></>}
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
  const { messages, draft, setDraft, busy, send, start, reset, agentMode, setAgentMode } = recommendation;
  const { toolsOpen = false, toolRequest, openTool, closeTools } = recommendation;
  const [draftInput, setDraftInput] = useState('');
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
  }, [messages, recommendation.error]);

  useEffect(() => {
    if (inputRef.current) {
      inputRef.current.style.height = 'auto';
      inputRef.current.style.height = `${Math.min(140, Math.max(48, inputRef.current.scrollHeight))}px`;
    }
  }, [draft]);

  useEffect(() => {
    const closeOutside = (event) => { if (optionsRef.current && !optionsRef.current.contains(event.target)) optionsRef.current.open = false; };
    document.addEventListener('pointerdown', closeOutside);
    return () => document.removeEventListener('pointerdown', closeOutside);
  }, []);

  const closeOptions = () => { if (optionsRef.current) optionsRef.current.open = false; };
  const newConversation = () => { closeOptions(); reset(); inputRef.current?.focus(); };

  return <div className={`ai-chat-workbench ${toolsOpen ? 'ai-chat-workbench-tools-open' : ''}`}><section className="panel ai-chat" aria-label="AI 추천 답변" onKeyDown={(event) => {
    if (event.key === 'Escape') { closeOptions(); }
  }}>
    <div className="ai-chat-header">
      <div className="ai-chat-identity">
        <span className="ai-chat-avatar" aria-hidden="true">AI</span>
        <div>
          <h2>민원 상담 도우미</h2>
          <p><span className="ai-chat-status-dot" />공식 자료를 바탕으로 안내해 드려요</p>
        </div>
      </div>
      
      {/* 우측 상단 모드 스위치 및 옵션 메뉴 */}
      <div className="ai-chat-header-actions" style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
        {recommendation.canCancel && <button className="danger" onClick={recommendation.cancelAnswer}>답변 요청 취소</button>}
        <div className="agent-mode-switch" style={{ display: 'flex', alignItems: 'center', background: '#f1f3f5', padding: '3px 6px', borderRadius: '6px' }}>
          <button 
            onClick={() => setAgentMode(0)} 
            style={{ 
              padding: '4px 10px', 
              borderRadius: '4px', 
              border: 'none', 
              background: agentMode === 0 ? '#007bff' : 'transparent', 
              color: agentMode === 0 ? '#fff' : '#333',
              cursor: 'pointer',
              fontSize: '12px',
              fontWeight: agentMode === 0 ? 'bold' : 'normal'
            }}
          >
            민원처리모드
          </button>
          <button 
            onClick={() => setAgentMode(1)} 
            style={{ 
              padding: '4px 10px', 
              borderRadius: '4px', 
              border: 'none', 
              background: agentMode === 1 ? '#007bff' : 'transparent', 
              color: agentMode === 1 ? '#fff' : '#333',
              cursor: 'pointer',
              fontSize: '12px',
              fontWeight: agentMode === 1 ? 'bold' : 'normal'
            }}
          >
            문의모드
          </button>
        </div>

        <details className="ai-chat-options" ref={optionsRef}>
          <summary aria-label="대화 옵션"><span aria-hidden="true">⋯</span> 옵션</summary>
          <div className="ai-chat-options-menu">
            <button disabled={busy} onClick={newConversation}>새 대화 시작</button>
            <button onClick={() => { closeOptions(); openTool('create'); }}>새로운 민원 작성하기<small>오른쪽 도구에서 작성하고 접수</small></button>
            <button onClick={() => { closeOptions(); openTool('list'); }}>작성된 민원에서 선택하기<small>오른쪽 도구에서 확인·수정·삭제</small></button>
            <button disabled={busy} onClick={() => { closeOptions(); recommendation.promptLookup('filter'); }}>특정 민원 조회<small>부서와 처리 상태로 골라 보기</small></button>
            <details className="ai-chat-usage"><summary>이용 안내</summary><p>대화로 민원 작성이나 확인을 요청하면 오른쪽 도구를 열어 드립니다. ‘조회하고 싶어’는 부서·상태 선택, ‘검색하고 싶어’는 다음 메시지 한 번으로 작성한 민원을 찾습니다. 실제 접수·수정·삭제는 도구 창에서 버튼으로 확정합니다.</p><p>다른 메뉴로 이동해도 대화는 유지됩니다. 새로고침하거나 로그아웃하면 화면의 대화는 초기화됩니다.</p></details>
          </div>
        </details>
      </div>
    </div>

    <div className="ai-chat-history" ref={scrollRef} role="log" aria-label="민원 상담 대화" aria-live="polite" aria-relevant="additions text" onScroll={(event) => {
      const element = event.currentTarget;
      nearBottom.current = element.scrollHeight - element.scrollTop - element.clientHeight < 100;
    }}>
      <div className="ai-chat-date">오늘</div>
      <article className="ai-chat-message ai-chat-message-assistant">
        <span className="ai-chat-avatar" aria-hidden="true">AI</span>
        <div className="ai-chat-message-body"><span className="ai-chat-sender">민원 상담 도우미</span><div className="ai-chat-bubble"><p className="ai-message-text">
          안녕하세요! 어떤 도움이 필요하신가요?
          { '\n' }<b>민원처리모드</b>에서는 민원의 접수, 조회, 수정, 삭제가 가능하고
          { '\n' }<b>문의 모드</b>에서는 민원과 관련된 문의를 제가 답변해드려요!
          { '\n' }
          { '\n' }<b>민원처리모드 기능</b>
          { '\n' }민원을 접수하고 싶은 경우 접수하고 싶은 민원의 구체적인 내용과 함께 접수하고 싶다고 말씀하시면 됩니다.
          { '\n' }민원을 조회, 수정, 삭제하고 싶으신 경우에는 찾고자 하는 민원의 구체적인 내용과 함께 그 민원을 찾고 싶다고 말씀하시면 됩니다.
          </p></div></div>
      </article>
      {messages.map((message) => message.role === 'user'
        ? <article key={message.id} className="ai-chat-message ai-chat-message-user" aria-label="내 메시지"><div className="ai-chat-message-body"><span className="ai-chat-sender">나</span><div className="ai-chat-bubble">{message.showTitle && message.title && <strong className="ai-chat-topic">{message.title}</strong>}<p className="ai-message-text">{message.content || '선택한 민원을 불러오고 있어요.'}</p></div></div></article>
        : <AssistantMessage key={message.id} message={message} recommendation={recommendation} />)}
    </div>

    <div className="ai-chat-bottom">
      <form className="ai-chat-compose" aria-label="메시지 보내기" onSubmit={(event) => { event.preventDefault(); if (!busy) send(); }}>
        <label className="ai-chat-input-label" htmlFor="ai-chat-input">메시지</label>
        <textarea id="ai-chat-input" ref={inputRef} rows={1} maxLength={recommendation.lookupPrompt?.kind === 'search' ? 1000 : 4000} value={draft} disabled={busy} onChange={(event) => setDraft(event.target.value)} placeholder={busy ? '처리 결과를 기다리고 있어요…' : recommendation.lookupPrompt?.kind === 'search' ? '찾고 싶은 민원의 상황이나 내용을 입력해 주세요' : messages.length ? '이어서 궁금한 점을 물어보세요' : '상황이나 궁금한 점을 입력해 주세요'} onCompositionStart={() => { composing.current = true; }} onCompositionEnd={() => { composing.current = false; }} onKeyDown={(event) => {
          if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing && !composing.current && event.keyCode !== 229) {
            event.preventDefault(); if (!busy) send();
          }
        }} />
        <button className="ai-chat-send" type="submit" disabled={busy || !draft.trim()}><span aria-hidden="true">↑</span><span>전송</span></button>
      </form>
      <p className="ai-chat-disclaimer">AI 안내는 참고용이며 담당 기관의 공식 답변과 다를 수 있습니다.</p>
    </div>
  </section>

  {/* 원래 구조인 우측 도구 패널 (CitizenComplaintTools)을 통해 title과 content가 자동으로 폼에 채워집니다 */}
  {toolRequest && <div id="ai-citizen-tool-window" className="ai-chat-tool-slot" hidden={!toolsOpen}>
    <CitizenComplaintTools api={api} request={toolRequest} visible={toolsOpen} onContextChange={recommendation.setToolContext}
      onClose={closeTools} recommendationBusy={busy} onChanged={(event) => { recommendation.notifyToolChanged(event); onToolsChanged?.(event); }}
      onRecommend={(record) => record.id
        ? start({ complaint_id: record.id }, { title: record.title, content: record.content, showTitle: true })
        : start({ title: record.title || '작성 중인 민원', content: record.content }, { showTitle: true })} />
  </div>}</div>;
}
