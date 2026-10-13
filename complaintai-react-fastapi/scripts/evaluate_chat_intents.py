"""Real Ollama intent evaluation only; never starts the application or writes complaints."""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import settings
from app.chat_intent import ChatIntentBody, PROMPT_VERSION, SYSTEM_PROMPT, classify_chat_intent

CASES = [
    {'id': 'create', 'message': '내가 민원을 작성하고 싶어', 'expected_intent': 'create'},
    {'id': 'list', 'message': '내가 쓴 민원을 확인하고 싶어', 'expected_intent': 'list'},
    {'id': 'edit-explicit', 'message': '#42 민원을 수정하고 싶어', 'expected_intent': 'edit', 'expected_id': 42},
    {'id': 'delete-explicit', 'message': '42번 민원을 삭제하고 싶어', 'expected_intent': 'delete', 'expected_id': 42},
    {'id': 'ordinary-question', 'message': '아파트 층간소음은 어디에 상담하나요?', 'expected_intent': 'answer'},
    {'id': 'writing-procedure', 'message': '민원은 어떻게 작성하나요?', 'expected_intent': 'answer'},
    {'id': 'deleting-procedure', 'message': '작성한 민원을 삭제하는 방법이 궁금해요', 'expected_intent': 'answer'},
    {'id': 'negative-delete', 'message': '민원을 삭제하지 마', 'expected_intent': 'answer'},
    {'id': 'mixed-negation', 'message': '수정 말고 내가 쓴 민원 목록을 보여줘', 'expected_intent': 'list'},
    {'id': 'close-tools', 'message': '오른쪽 도구창 닫아줘', 'expected_intent': 'close_tools'},
    {'id': 'context-edit', 'message': '이 민원을 수정하고 싶어',
     'tool_context': {'mode': 'list', 'complaint_id': 42, 'title': '가로등 고장', 'content': '집 앞 가로등이 꺼져 있습니다.'},
     'expected_intent': 'edit', 'expected_id': 42},
]


async def evaluate(args):
    path = Path(args.output).resolve()
    if path.exists():
        report = json.loads(path.read_text(encoding='utf-8'))
    else:
        report = {'purpose': 'actual_model_intent_routing_evaluation_only', 'complaint_database_writes': False, 'rounds': []}
    selected = [case for case in CASES if not args.only or case['id'] in args.only.split(',')]
    if not selected:
        raise ValueError('No matching cases')
    round_result = {
        'label': args.label, 'started_at': datetime.now(timezone.utc).isoformat(),
        'model': settings.RAG_LLM_MODEL, 'prompt_version': PROMPT_VERSION,
        'prompt_sha256': hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest(),
        'temperature': 0, 'think': False, 'num_ctx': settings.RAG_NUM_CTX,
        'cases': [],
    }
    report['rounds'].append(round_result)
    path.parent.mkdir(parents=True, exist_ok=True)

    def save():
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

    save()
    for index, case in enumerate(selected):
        print(f"[{index + 1}/{len(selected)}] {case['id']} START", flush=True)
        started = time.monotonic()
        entry = {**case}
        try:
            result = await classify_chat_intent(ChatIntentBody.model_validate({
                key: value for key, value in case.items() if key in ('message', 'tool_context', 'recent_messages')}))
            entry['prediction'] = result.model_dump()
            entry['passed'] = result.intent == case['expected_intent'] and result.complaint_id == case.get('expected_id')
        except Exception as exc:
            entry['error'] = {'type': type(exc).__name__, 'message': str(exc)}
            entry['passed'] = False
        entry['seconds'] = round(time.monotonic() - started, 2)
        round_result['cases'].append(entry)
        save()
        print(f"{case['id']}: {entry.get('prediction', entry.get('error'))} passed={entry['passed']} seconds={entry['seconds']}", flush=True)
    round_result['completed_at'] = datetime.now(timezone.utc).isoformat()
    round_result['passed'] = sum(case['passed'] for case in round_result['cases'])
    round_result['total'] = len(round_result['cases'])
    save()
    print(f"SUMMARY {round_result['passed']}/{round_result['total']} -> {path}", flush=True)
    return round_result['passed'] == round_result['total']


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', default=str(ROOT.parent / 'docs/ai-expected-answer/chat-intent-evaluation-2026-10-13.json'))
    parser.add_argument('--label', default='initial')
    parser.add_argument('--only', help='Comma-separated case IDs, preserving prior rounds in the output file')
    args = parser.parse_args()
    sys.exit(0 if asyncio.run(evaluate(args)) else 1)
