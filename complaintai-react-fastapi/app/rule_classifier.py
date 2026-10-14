"""Context-aware, weighted first-stage department classification.

These confidence values are policy indicators, not measured probabilities.
One specific phrase weighs 3; ordinary terms weigh 1. Auto-confirm requires a
score of 3 and a margin of 2. Zero evidence remains a separate catch-all case;
bulk CSV's existing no-evidence policy is controlled by the import caller.
"""
from dataclasses import dataclass
from functools import lru_cache
import re

RULE_VERSION = 'context-weighted-v1'
MIN_CONFIDENT_SCORE = 3
MIN_CONFIDENT_MARGIN = 2
SPECIFIC_WEIGHT = 3

# Spaces are normalized for lookup, while patterns accept spaced/unspaced input.
SPECIFIC_KEYWORDS = {
    '노동·기업': {'산업안전', '고용지원금', '공정거래', '경영전략'},
    '교통·국토': {'교통신호', '도로보수', '건설현장', '건설현장품질', '건설현장안전', '도시계획', '개발행위', '건설업등록'},
    '주택·건축': {'건축허가', '건축방화', '건축피난', '건축구조', '공동주택', '층간소음', '임대보증금', '전세보증금'},
    '환경·위생': {'미세먼지', '상하수도', '대기오염', '대기질', '공사장소음', '공장소음'},
    '문화·행정·안전': {'민원행정', '생활서비스', '공공서비스', '소화전', '민방위', '지역화폐'},
    '보건·복지': {'복지급여', '여성가족', '감염병'},
}
CONTEXT_PATTERNS = {
    ('환경·위생', '대기'): (
        r'오염|미세먼지|매연|분진|배출가스|유해가스|대기\s*질|환경',
        r'대기\s*(?:시간|순번|번호|인원|상태|중|줄|표)',
    ),
    ('교통·국토', '인도'): (
        r'보행|보도|차도|도로|횡단|점자|휠체어|통행|걷|걸어|다니|파손|포장|구멍|적치|침범',
        r'인도(?:네시아|양|받|하|해|했|할)|(?:상품|물품|제품|목적물).{0,8}인도|인도.{0,8}(?:기한|납기|인수|배송)',
    ),
    ('교통·국토', '공사'): (
        r'공사\s*(?:장|현장)|착공|시공|준공|굴착|토목|도로|보수|건설|현장',
        r'한국\s*(?:토지주택|철도|관광)\s*공사',
    ),
}
SPECIAL_PATTERNS = {
    ('환경·위생', '공사장소음'): r'공사장[^.!?。\n]{0,18}소음',
    ('환경·위생', '공장소음'): r'공장[^.!?。\n]{0,18}소음',
}


@lru_cache(maxsize=512)
def keyword_pattern(category, keyword):
    canonical = re.sub(r'\s+', '', keyword)
    expression = SPECIAL_PATTERNS.get((category, canonical))
    # Permit optional spaces between Hangul characters: 층간소음 / 층간 소음,
    # 고용지원금 / 고용 지원금 are one concept, not independent score sources.
    if expression is None:
        expression = r'\s*'.join(re.escape(character) for character in canonical)
    return re.compile(expression, re.IGNORECASE)


@lru_cache(maxsize=32)
def context_patterns(category, canonical):
    patterns = CONTEXT_PATTERNS.get((category, canonical))
    return tuple(re.compile(value, re.IGNORECASE) for value in patterns) if patterns else ()


@dataclass(frozen=True)
class Hit:
    category: str
    keyword: str
    start: int
    end: int
    weight: int


def classify_rules(source: str, keywords: dict[str, list[str]]) -> dict:
    hits, ignored = [], []
    for category, terms in keywords.items():
        seen = set()
        for keyword in terms:
            canonical = re.sub(r'\s+', '', keyword)
            if not canonical or canonical in seen:
                continue
            seen.add(canonical)
            patterns = context_patterns(category, canonical)
            blocked = False
            for match in keyword_pattern(category, keyword).finditer(source):
                # Context must surround this occurrence, not an unrelated page or
                # sentence. A rejected first occurrence does not reject later ones.
                window = source[max(0, match.start() - 24):match.end() + 24]
                if patterns and (not patterns[0].search(window) or patterns[1].search(window)):
                    blocked = True
                    continue
                weight = SPECIFIC_WEIGHT if canonical in SPECIFIC_KEYWORDS.get(category, set()) else 1
                hits.append(Hit(category, keyword, match.start(), match.end(), weight))
                break  # Repetition never adds points.
            else:
                if blocked:
                    ignored.append({'category': category, 'keyword': keyword})

    # Do not count 건축 + 건축구조, or 고용 + 고용지원금 as two signals.
    # A specific 층간소음 signal also suppresses the contained generic 소음
    # signal in another department. Equal-strength cross-department hits remain
    # eligible so a real tie is routed to the LLM instead of being hidden.
    retained = [hit for hit in hits if not any(
        other.start <= hit.start and other.end >= hit.end and
        (other.start < hit.start or other.end > hit.end) and
        (other.weight > hit.weight or (other.category == hit.category and other.weight >= hit.weight))
        for other in hits
    )]
    scores = {category: 0 for category in keywords}
    matched = {category: [] for category in keywords}
    for hit in retained:
        scores[hit.category] += hit.weight
        matched[hit.category].append({'keyword': hit.keyword, 'weight': hit.weight})
    ranked = sorted(scores, key=scores.get, reverse=True)
    top = ranked[0]
    best = scores[top]
    runner_up = scores[ranked[1]] if len(ranked) > 1 else 0
    margin = best - runner_up
    ambiguous_deposit = bool(re.search(r'보증금', source) and not re.search(r'(?:임대|전세)\s*보증금', source))
    reasons = []
    if best == 0:
        reasons.append('no_keyword')
    elif best < MIN_CONFIDENT_SCORE:
        reasons.append('weak_score')
    if best and margin < MIN_CONFIDENT_MARGIN:
        reasons.append('tie' if margin == 0 else 'small_margin')
    if ambiguous_deposit:
        reasons.append('ambiguous_deposit')
    requires_llm = any(reason != 'no_keyword' for reason in reasons)
    confidence = 0.3 if not best else (0.4 if requires_llm else 0.85)
    return {'version': RULE_VERSION, 'category': top if best else '기타',
            'confidence': confidence, 'scores': scores, 'top_score': best,
            'runner_up_score': runner_up, 'margin': margin,
            'matched': matched, 'ignored_context': ignored,
            'requires_llm': requires_llm, 'reasons': reasons}
