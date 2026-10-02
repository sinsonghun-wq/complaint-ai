import unittest
from unittest.mock import MagicMock, mock_open, patch

from app.worker_manager import ensure_csv_worker
from app.worker import run


class WorkerManagerTests(unittest.TestCase):
    def test_existing_worker_is_not_spawned(self):
        with patch("app.worker_manager.worker_is_running", return_value=True), patch("app.worker_manager.subprocess.Popen") as spawn:
            ensure_csv_worker()
        spawn.assert_not_called()

    def test_missing_worker_starts_and_waits_for_ready(self):
        with patch("app.worker_manager.worker_is_running", side_effect=[False, True]), patch("app.worker_manager.Path.open", mock_open()), patch("app.worker_manager.subprocess.Popen") as spawn:
            ensure_csv_worker()
        self.assertEqual(spawn.call_args.args[0][-2:], ["-m", "app.worker"])

    def test_start_failure_is_reported(self):
        with patch("app.worker_manager.worker_is_running", return_value=False), patch("app.worker_manager.Path.open", mock_open()), patch("app.worker_manager.subprocess.Popen") as spawn:
            spawn.return_value.poll.return_value = 1
            with self.assertRaises(RuntimeError):
                ensure_csv_worker()

    def test_second_worker_exits_without_claiming_jobs(self):
        lease = MagicMock()
        lease.cursor.return_value.__enter__.return_value.fetchone.return_value = {"acquired": False}
        with patch("app.worker.connection") as connect, patch("app.worker.run_loop") as loop:
            connect.return_value.__enter__.return_value = lease
            run()
        loop.assert_not_called()
