"""Independent reference importer; complaint CSV jobs keep their own worker."""
from pathlib import Path
import subprocess
import sys
import threading
import time

from .db import connection

WORKER_LOCK_KEY = 831947264
_launch_lock = threading.Lock()
_process = None


def worker_is_running():
    with connection() as db:
        acquired = db.execute("SELECT pg_try_advisory_lock(%s) acquired", (WORKER_LOCK_KEY,)).fetchone()["acquired"]
        if acquired:
            db.execute("SELECT pg_advisory_unlock(%s)", (WORKER_LOCK_KEY,))
        return not acquired


def ensure_rag_worker():
    global _process
    with _launch_lock:
        if worker_is_running():
            return
        root = Path(__file__).resolve().parents[1]
        executable = root / ".venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
        flags = (subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP) if sys.platform == "win32" else 0
        with (root / "data/rag-worker.stdout.log").open("ab") as stdout, (root / "data/rag-worker.stderr.log").open("ab") as stderr:
            _process = subprocess.Popen([str(executable), "-m", "app.rag_worker"], cwd=root, stdin=subprocess.DEVNULL,
                                        stdout=stdout, stderr=stderr, creationflags=flags, start_new_session=sys.platform != "win32")
        for _ in range(40):
            if worker_is_running():
                return
            if _process.poll() is not None:
                break
            time.sleep(0.25)
        raise RuntimeError("RAG 자료 처리 워커를 시작하지 못했습니다.")
