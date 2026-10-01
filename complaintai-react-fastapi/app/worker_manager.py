"""Start the local CSV worker on demand; PostgreSQL enforces one live worker."""
from pathlib import Path
import subprocess
import sys
import threading
import time

from .db import connection

WORKER_LOCK_KEY = 831947261
_launch_lock = threading.Lock()
_process = None


def worker_is_running() -> bool:
    with connection() as db:
        with db.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s) AS acquired", (WORKER_LOCK_KEY,))
            acquired = cur.fetchone()["acquired"]
            if acquired:
                cur.execute("SELECT pg_advisory_unlock(%s)", (WORKER_LOCK_KEY,))
            return not acquired


def ensure_csv_worker() -> None:
    global _process
    with _launch_lock:
        if worker_is_running():
            return
        root = Path(__file__).resolve().parents[1]
        executable = root / ".venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
        if not executable.is_file():
            executable = Path(sys.executable)
        flags = (subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP) if sys.platform == "win32" else 0
        with (root / "data" / "csv-worker.stdout.log").open("ab") as stdout, (root / "data" / "csv-worker.stderr.log").open("ab") as stderr:
            _process = subprocess.Popen([str(executable), "-m", "app.worker"], cwd=root, stdin=subprocess.DEVNULL,
                                        stdout=stdout, stderr=stderr, creationflags=flags,
                                        start_new_session=sys.platform != "win32")
        for _ in range(40):
            if worker_is_running():
                return
            if _process.poll() is not None:
                raise RuntimeError("CSV 워커가 시작 중 종료되었습니다. 워커 오류 기록을 확인해 주세요.")
            time.sleep(0.25)
        raise RuntimeError("CSV 워커 시작을 확인하지 못했습니다. 잠시 후 재처리해 주세요.")
