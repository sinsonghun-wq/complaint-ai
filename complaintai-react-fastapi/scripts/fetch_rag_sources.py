"""Fetch a small official HTML pilot corpus; never index catalog summaries."""
from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = ROOT / 'complaintai-react-fastapi/data/rag/official-pilot'
PARSERS = {'A04': '110', 'A06': '110', 'T02': 'interpretation', 'H02': 'floor-faq', 'E02': 'food', 'W07': 'medical-faq', 'F06': 'fire-faq'}


def node_text(node) -> str:
    if node is None:
        raise ValueError('Expected official content element not found')
    copy = BeautifulSoup(str(node), 'html.parser')
    for tag in copy.select('script,style,.blind'):
        tag.decompose()
    for table in copy.find_all('table'):
        rows = [' | '.join(cell.get_text(' ', strip=True) for cell in row.find_all(['th', 'td'])) for row in table.find_all('tr')]
        table.replace_with('\n' + '\n'.join(rows) + '\n')
    for tag in copy.find_all(['br']):
        tag.replace_with('\n')
    for tag in copy.find_all(['p', 'li', 'div']):
        tag.insert_after('\n')
    text = copy.get_text(' ', strip=True).replace('\xa0', ' ')
    return '\n'.join(re.sub(r'[ \t]+', ' ', line).strip() for line in text.splitlines() if line.strip())


def parse_official_html(html: bytes, parser: str) -> list[dict]:
    soup = BeautifulSoup(html, 'html.parser')
    sections = []
    if parser == '110':
        node = soup.select_one('.boardBody')
        sections = [{'heading': soup.select_one('.questionBox').get_text(' ', strip=True) if soup.select_one('.questionBox') else '공식 상담 FAQ', 'text': node_text(node), 'locator': '질문내용·답변'}]
    elif parser == 'interpretation':
        text = node_text(soup.select_one('#contentBody'))
        if '【회답】' not in text:
            raise ValueError('Official interpretation answer is missing')
        # The question/title is not a factual answer. Preserve only the official reply and caveats.
        text = '【회답】' + text.split('【회답】', 1)[1]
        sections = [{'heading': '공식 회답·관련법령·해석 유의사항', 'text': text, 'locator': '회답 및 중앙부처 1차 해석 안내'}]
    elif parser == 'floor-faq':
        for index, item in enumerate(soup.select('.faq_list > dl'), 1):
            sections.append({'heading': node_text(item.find('dt')), 'text': node_text(item), 'locator': f'FAQ Q{index}',
                'applicability': {'building_scope': 'non_shared_housing' if index == 12 else 'shared_housing'}})
    elif parser == 'food':
        nodes = soup.select('#mb_wrap .bs-callout, #mb_wrap .emphasis, #mb_wrap .join_new_step')
        sections = [{'heading': '소비자 신고 대상·조사 요건·신고 절차', 'text': '\n'.join(node_text(n) for n in nodes), 'locator': '소비자 신고 안내'}]
    elif parser == 'medical-faq':
        for question in soup.select('tr.question'):
            link = question.select_one('a.faq_click')
            answer = soup.select_one('#' + link['aria-controls'])
            heading = node_text(link)
            sections.append({'heading': heading, 'text': heading + '\n' + node_text(answer.select_one('.answer_content')), 'locator': 'FAQ ' + link['data-seq']})
    elif parser == 'fire-faq':
        for index, item in enumerate(soup.select('.fire-info-faq dl'), 1):
            sections.append({'heading': node_text(item.find('dt')), 'text': node_text(item), 'locator': '安심콜 FAQ ' + str(index)})
    else:
        raise ValueError('Unsupported parser')
    if not sections or any(len(item['text']) < 40 for item in sections):
        raise ValueError('Official answer content is missing; page may be a listing or challenge')
    return sections


async def fetch(output: Path) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    sources = list(csv.DictReader((ROOT / 'docs/ai-expected-answer/sources.csv').open(encoding='utf-8-sig')))
    types = list(csv.DictReader((ROOT / 'docs/ai-expected-answer/complaint-types.csv').open(encoding='utf-8-sig')))
    retrieved_at = datetime.now(timezone.utc).isoformat()
    documents, errors = [], []
    async with httpx.AsyncClient(timeout=45, follow_redirects=True) as client:
        for source in sources:
            source_id = source['source_id']
            if source_id not in PARSERS:
                continue
            try:
                response = await client.get(source['url'])
                response.raise_for_status()
                if 'text/html' not in response.headers.get('content-type', ''):
                    raise ValueError('Expected official HTML')
                sections = parse_official_html(response.content, PARSERS[source_id])
                original_hash = hashlib.sha256(response.content).hexdigest()
                section_json = json.dumps(sections, ensure_ascii=False, indent=2)
                section_hash = hashlib.sha256(section_json.encode()).hexdigest()
                raw_path = output / (source_id + '-' + original_hash[:16] + '.html')
                raw_path.write_bytes(response.content)
                section_path = output / (source_id + '-' + section_hash[:16] + '.sections.json')
                section_path.write_text(section_json, encoding='utf-8')
                related = [t for t in types if source_id in t['source_ids'].split(';')]
                license_note = '공공누리 출처표시·상업적이용금지·변경금지 표기; 이용 범위 별도 확인' if source_id in ('A04', 'A06') else '공식 페이지의 이용 조건 확인'
                documents.append({
                    'source_id': source_id, 'title': source['title'], 'official_url': str(response.url),
                    'publisher': source['publisher_and_host'], 'kind': source['kind'], 'version': source['edition_or_date'],
                    'jurisdiction': '전국', 'effective_from': None, 'effective_to': None,
                    'retrieved_at': retrieved_at, 'original_path': raw_path.name, 'sections_path': section_path.name,
                    'original_hash': original_hash,
                    'departments': sorted({t['department'] for t in related}), 'type_ids': [t['type_id'] for t in related],
                    'review_status': 'verified', 'verification_method': 'official_html_answer_sections_extracted',
                    'limitations': source['limitations'], 'license_note': license_note,
                })
                print(f'{source_id}: {len(sections)} official answer sections', flush=True)
            except Exception as exc:
                errors.append({'source_id': source_id, 'error': str(exc)})
                print(f'{source_id}: skipped ({exc})', flush=True)
    manifest = {'retrieved_at': retrieved_at, 'scope': 'Official HTML pilot; not comprehensive coverage of 32 types', 'documents': documents, 'errors': errors}
    (output / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    result = asyncio.run(fetch(args.output.resolve()))
    print(json.dumps({'documents': len(result['documents']), 'errors': result['errors']}, ensure_ascii=False))
    if not result['documents']:
        raise SystemExit(1)
