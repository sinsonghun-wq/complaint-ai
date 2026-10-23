"""Lossless source spans and the exact, pinned BGE-M3 tokenizer; no LLM calls."""
from __future__ import annotations

import hashlib
import os
import re
import threading
from functools import lru_cache
from pathlib import Path
from urllib.request import urlopen

from tokenizers import Tokenizer

from ..settings import ACTIVE_EMBEDDING_MODEL, ROOT

TOKEN_LIMIT = 8192
CHUNK_TARGET = 7500
SPLITTER_VERSION = 'bge-m3-law-spans-v1'
TOKENIZER_SHA256 = '21106b6d7dab2952c1d496fb21d5dc9db75c28ed361a05f5020bbba27810dd08'
TOKENIZER_URL = 'https://huggingface.co/BAAI/bge-m3/resolve/5617a9f61b028005a4858fdac845db406aefb181/tokenizer.json'
_lock = threading.Lock()
ARTICLE = r'제\s*\d+\s*조(?:\s*의\s*\d+)?'
ANNEX = r'별표\s*\d+(?:\s*의\s*\d+)?'
HEADING = re.compile(rf'(?m)(?<![가-힣A-Za-z0-9])({ARTICLE}|{ANNEX})[ \t]*(?:[(（][^\n)）]{{1,120}}[)）](?![ \t]*(?:에[ \t]*(?:따라|따른|도|서)|의[ \t]*규정|[을를](?:[ \t]|$)))|(?=\])|(?=$))')
REFERENCE = re.compile(rf'({ARTICLE}|{ANNEX})')
EXTERNAL = re.compile(rf'(?:[「『“"]([^」』”"]+)[」』”"]|(?<![가-힣])([가-힣A-Za-z0-9·ㆍ]+(?:법률|시행령|시행규칙|조례|규칙|법)))\s*({ARTICLE}|{ANNEX})')


@lru_cache(maxsize=1)
def tokenizer():
    if ACTIVE_EMBEDDING_MODEL.split(':')[0].lower() not in ('bge-m3', 'baai/bge-m3'):
        raise ValueError('현재 임베딩 모델과 일치하는 RAG 토크나이저가 필요합니다. 모델을 임의로 바꾸지 않습니다.')
    path = Path(os.getenv('RAG_TOKENIZER_PATH', str(ROOT / 'data/tokenizers/bge-m3.json')))
    with _lock:
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            with urlopen(TOKENIZER_URL, timeout=30) as response:
                data = response.read(24 * 1024 * 1024)
            if hashlib.sha256(data).hexdigest() != TOKENIZER_SHA256:
                raise ValueError('BGE-M3 토크나이저 무결성 검증에 실패했습니다.')
            temporary = path.with_suffix(f'.{os.getpid()}.tmp')
            temporary.write_bytes(data)
            temporary.replace(path)
        if hashlib.sha256(path.read_bytes()).hexdigest() != TOKENIZER_SHA256:
            raise ValueError('BGE-M3 토크나이저 파일이 공식 고정 버전과 다릅니다.')
        result = Tokenizer.from_file(str(path))
        result.no_truncation(); result.no_padding()
        return result


def token_count(text):
    return len(tokenizer().encode(text).ids)


def key(text):
    return re.sub(r'\s+', '', text)


def document_structure(text):
    headings = list(HEADING.finditer(text))
    if headings:
        starts = [(match.start()-1 if match.start() and text[match.start()-1]=='[' else match.start(), key(match.group(1)), match.group().strip()[:160]) for match in headings]
        if starts[0][0]: starts.insert(0, (0, 'preamble', '서문'))
    else:
        # No legal headings: preserve paragraphs, not a fabricated law/article identity.
        positions = [0]
        for match in re.finditer(r'\n\s*\n|(?<=[.!?。])\s+', text):
            if match.end() - positions[-1] >= 1500: positions.append(match.end())
        bounded = [0]
        for end in positions[1:] + [len(text)]:
            while end - bounded[-1] > 3000: bounded.append(bounded[-1] + 3000)
            if end < len(text) and end > bounded[-1]: bounded.append(end)
        positions = bounded
        starts = [(position, f'paragraph:{i}', f'문단 {i+1}') for i, position in enumerate(positions) if position < len(text)]
    units = []
    for i, (start, identifier, heading) in enumerate(starts):
        end = starts[i+1][0] if i+1 < len(starts) else len(text)
        body = text[start:end]
        external_matches = [m for m in EXTERNAL.finditer(body) if (m.group(1) or m.group(2)) not in ('법','법률')]
        external = [{'law': m.group(1) or m.group(2), 'key': key(m.group(3))} for m in external_matches]
        external_spans = [m.span() for m in external_matches]
        references = list(dict.fromkeys(key(m.group()) for m in REFERENCE.finditer(body)
                                       if not any(a <= m.start() < b for a, b in external_spans)))
        if '전조' in body and i: references.append(starts[i-1][1])
        if '다음 조' in body and i+1 < len(starts): references.append(starts[i+1][1])
        units.append(dict(key=identifier, heading=heading, start=start, end=end,
                          references=references, external=external,
                          exception=bool(re.search('다만|예외|제외|불구하고|적용하지', body))))
    return units


def split_document(text, target=CHUNK_TARGET):
    """Spans cover every original character exactly once; metadata carries context."""
    encoded = tokenizer().encode(text, add_special_tokens=False)
    offsets = encoded.offsets
    units = document_structure(text)
    boundaries = sorted({0, len(text)} | {u['start'] for u in units} |
                        {m.end() for m in re.finditer(r'\n|[.!?。][ \t]+', text)})
    unit_ends = [u['end'] for u in units]
    if not HEADING.search(text): target = min(target, 2048)
    from bisect import bisect_right
    start = 0; index = 0
    while start < len(text):
        # Advancing token cursor makes planning linear even for million-token input.
        while index < len(offsets) and offsets[index][1] <= start: index += 1
        last = min(len(offsets)-1, index + target - 3)
        end = len(text) if last >= len(offsets)-1 else offsets[last][1]
        end = max(start+1, end)
        unit_end = unit_ends[bisect_right(unit_ends, start)]
        if unit_end <= end: end = unit_end
        if end < len(text):
            candidate = boundaries[bisect_right(boundaries, end)-1]
            if candidate > start: end = candidate
        # Token boundaries can change when a substring is encoded independently.
        while token_count(text[start:end]) > target:
            end = start + max(1, (end-start)*9//10)
        yield dict(start_offset=start, end_offset=end, content=text[start:end], token_count=token_count(text[start:end]))
        start = end
