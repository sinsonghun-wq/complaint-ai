"""Focused retrieval review for an existing evaluation case; no answer generation."""
import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.rag import retrieve_candidates, retrieval_questions
from app.rag_models import embed_texts, embedding_identity, rerank


async def review(case_id, question_index, output):
    cases = json.loads((Path(__file__).resolve().parents[1] / 'tests/fixtures/rag-evaluation.json').read_text(encoding='utf-8'))
    case = next(case for case in cases if case['id'] == case_id)
    questions = retrieval_questions(case['title'], case['content'])
    question = questions[question_index]
    identity = await embedding_identity()
    vector = (await embed_texts([question], query=True))[0]
    candidates = retrieve_candidates(question, vector, identity['profile'])
    ranked, model = await rerank(question, [row['heading'] + '\n' + row['content'] for row in candidates])
    rows = [{'source_id': candidates[item['index']]['source_id'], 'heading': candidates[item['index']]['heading'], 'locator': candidates[item['index']]['locator'], 'score': item['relevance_score'], 'text': candidates[item['index']]['content']} for item in ranked]
    result = {'case_id': case_id, 'question_index': question_index, 'question': question, 'embedding': identity, 'reranker': model, 'ranked_candidates': rows, 'purpose': 'evaluation_only'}
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps([{key: value for key, value in row.items() if key != 'text'} for row in rows], ensure_ascii=False, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--case', required=True)
    parser.add_argument('--question-index', type=int, default=0)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    asyncio.run(review(args.case, args.question_index, args.output.resolve()))
