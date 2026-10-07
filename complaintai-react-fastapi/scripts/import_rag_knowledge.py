import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.rag_ingest import import_manifest

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('manifest', type=Path)
    args = parser.parse_args()
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    print(json.dumps(asyncio.run(import_manifest(args.manifest.resolve())), ensure_ascii=False, indent=2))
