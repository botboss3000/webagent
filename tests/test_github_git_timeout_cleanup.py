import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app.api import github


class GitTimeoutCleanupTests(unittest.TestCase):
    def test_run_git_waits_for_an_existing_webagent_git_call(self):
        entered = threading.Event()
        finished = threading.Event()
        popen = Mock()
        proc = Mock()
        proc.communicate.return_value = ("", "")
        proc.returncode = 0
        popen.return_value = proc

        def run_git():
            entered.set()
            github._run_git(["status", "--porcelain"], timeout=5)
            finished.set()

        with patch.object(github.subprocess, "Popen", popen):
            with github._GIT_COMMAND_LOCK:
                worker = threading.Thread(target=run_git)
                worker.start()
                self.assertTrue(entered.wait(timeout=1))
                time.sleep(0.02)
                popen.assert_not_called()
                self.assertFalse(finished.is_set())
            worker.join(timeout=1)

        self.assertTrue(finished.is_set())
        popen.assert_called_once()

    def test_run_git_timeout_kills_tree_and_cleans_its_index_lock(self):
        proc = Mock()
        proc.pid = 4321
        proc.communicate.side_effect = [
            subprocess.TimeoutExpired(cmd="git add -A", timeout=30),
            ("", ""),
        ]

        with (
            patch.object(github.subprocess, "Popen", return_value=proc) as popen,
            patch.object(github, "_index_lock_signature", return_value=None),
            patch.object(github, "_terminate_git_process_tree") as terminate,
            patch.object(github, "_cleanup_timed_out_index_lock") as cleanup,
        ):
            stdout, stderr, code = github._run_git(["add", "-A"], timeout=30)

        self.assertEqual((stdout, stderr, code), ("", "git command timed out", -1))
        terminate.assert_called_once_with(proc)
        cleanup.assert_called_once_with(["add", "-A"], None)

        popen_env = popen.call_args.kwargs["env"]
        self.assertEqual(popen_env["GIT_OPTIONAL_LOCKS"], "0")

    def test_timeout_cleanup_ignores_a_preexisting_index_lock(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            git_dir = root / ".git"
            git_dir.mkdir()
            lock = git_dir / "index.lock"
            lock.write_bytes(b"")

            with patch.object(github, "_ACTIVE_REPO", root):
                before = github._index_lock_signature()
                github._cleanup_timed_out_index_lock(["add", "-A"], before)

            self.assertTrue(lock.exists())

    def test_timeout_cleanup_removes_a_lock_created_by_our_command(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            git_dir = root / ".git"
            git_dir.mkdir()
            lock = git_dir / "index.lock"

            with patch.object(github, "_ACTIVE_REPO", root):
                before = github._index_lock_signature()
                lock.write_bytes(b"")
                github._cleanup_timed_out_index_lock(["add", "-A"], before)

            self.assertFalse(lock.exists())

    def test_timeout_cleanup_also_covers_read_only_commands(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            git_dir = root / ".git"
            git_dir.mkdir()
            lock = git_dir / "index.lock"

            with patch.object(github, "_ACTIVE_REPO", root):
                before = github._index_lock_signature()
                lock.write_bytes(b"")
                github._cleanup_timed_out_index_lock(["status", "--porcelain"], before)

            self.assertFalse(lock.exists())


if __name__ == "__main__":
    unittest.main()
