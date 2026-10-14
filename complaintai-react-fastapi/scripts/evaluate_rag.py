"""Real-model smoke evaluation. Heuristics do not replace expert answer review."""
import argparse
import asyncio
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.rag import recommend


async def evaluate(cases, output):
    results = []
    output.parent.mkdir(parents=True, exist_ok=True)
    for case in cases:
        start = time.monotonic()
        print('RUN ' + case['id'], flush=True)
        try:
            answer = await recommend(case['title'], case['content'], stage_callback=lambda stage: print(case['id'] + ': ' + stage, flush=True))
            citation_sources = {row['source_id'] for row in answer['citations']}
            combined = answer['answer'] + '\n' + '\n'.join(answer['next_steps'])
            checks = {
                'status_matches': answer['answer_status'] == case['expected_status'],
                'expected_source_found': not case['expected_sources'] or bool(set(case['expected_sources']) & citation_sources),
                'keywords_present': all(keyword in combined for keyword in case['keywords']),
                'answered_has_citations': answer['answer_status'] != 'answered' or bool(answer['citations']),
                'no_known_unsupported_claim': not any(re.search(pattern, combined) for pattern in case.get('forbidden_patterns', [])),
            }
            row = {'case': case, 'result': answer, 'automated_checks': checks, 'automated_pass': all(checks.values()), 'seconds': round(time.monotonic() - start, 2), 'human_review': 'pending'}
        except Exception as error:
            row = {'case': case, 'error': str(error), 'seconds': round(time.monotonic() - start, 2), 'automated_pass': False, 'human_review': 'pending'}
        results.append(row)
        output.write_text(json.dumps({'evaluated_at': datetime.now(timezone.utc).isoformat(), 'scope': 'Small pilot smoke test; not measured production accuracy', 'cases': results}, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({'id': case['id'], 'pass': row['automated_pass'], 'seconds': row['seconds'], 'error': row.get('error')}, ensure_ascii=False), flush=True)
    return results


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--cases', type=Path, default=Path(__file__).resolve().parents[1] / 'tests/fixtures/rag-evaluation.json')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--only', default='')
    args = parser.parse_args()
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    cases = json.loads(args.cases.read_text(encoding='utf-8'))
    if args.only:
        cases = [case for case in cases if case['id'] in args.only.split(',')]
    results = asyncio.run(evaluate(cases, args.output.resolve()))
    if not all(row['automated_pass'] for row in results):
        raise SystemExit(1)
