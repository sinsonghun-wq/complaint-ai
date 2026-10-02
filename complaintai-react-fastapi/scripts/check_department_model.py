"""Small live Ollama smoke test, not a statistical accuracy benchmark."""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.ai import analyze

async def main():
    samples = [
        ('노동', '임금 체불과 부당 해고에 대한 노동 민원입니다.'),
        ('교통', '버스 배차와 불법 주차 교통신호 문제를 처리해 주세요.'),
        ('건설·국토', '도로 포트홀과 교량 보수 토목 공사를 요청합니다.'),
    ]
    passed = True
    for expected, content in samples:
        print(json.dumps({'testing':expected}, ensure_ascii=True), flush=True)
        result = await analyze('분류 테스트', content)
        print(json.dumps({'expected':expected, 'category':result['category'], 'processing_mode':result['processing_mode']}, ensure_ascii=True), flush=True)
        passed &= result['category'] == expected and result['processing_mode'] == 'llm'
    return 0 if passed else 1

if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
