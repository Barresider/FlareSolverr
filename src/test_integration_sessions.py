"""
Integration tests for FlareSolverr session management — verifies real Chrome
processes are cleaned up at the OS level after session operations.

Run with: python -m pytest test_integration_sessions.py -v --timeout=120
Or: python -m unittest test_integration_sessions -v

These tests start REAL Chrome browsers, so they take 30-60 seconds total.
"""

import logging
import subprocess
import time
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import utils
from sessions import SessionsStorage

logging.basicConfig(level=logging.DEBUG)


def is_process_alive(pid: int) -> bool:
    """Check via tasklist if a PID is still running."""
    try:
        result = subprocess.run(
            ['tasklist', '/FI', f'PID eq {pid}'],
            capture_output=True,
            text=True,
            timeout=10
        )
        return str(pid) in result.stdout
    except Exception:
        return False


def force_kill_pid(pid: int):
    """Safety net — taskkill /F /T to kill a process tree."""
    try:
        subprocess.run(
            ['taskkill', '/F', '/T', '/PID', str(pid)],
            capture_output=True,
            text=True,
            timeout=10
        )
    except Exception:
        pass


def _get_all_chrome_pids() -> set[int]:
    """Snapshot all chrome.exe PIDs via tasklist CSV output."""
    try:
        result = subprocess.run(
            ['tasklist', '/FI', 'IMAGENAME eq chrome.exe', '/FO', 'CSV', '/NH'],
            capture_output=True,
            text=True,
            timeout=10
        )
        pids = set()
        for line in result.stdout.strip().splitlines():
            # CSV format: "chrome.exe","12345","Console","1","12,345 K"
            parts = line.split(',')
            if len(parts) >= 2:
                try:
                    pid = int(parts[1].strip('"'))
                    pids.add(pid)
                except ValueError:
                    continue
        return pids
    except Exception:
        return set()


class TestIntegrationSessions(unittest.TestCase):
    """Integration tests that start real Chrome and verify OS-level cleanup."""

    def setUp(self):
        utils.get_current_platform()
        self.storage = SessionsStorage()
        self._tracked_pids = []

    def tearDown(self):
        # Destroy any remaining sessions
        for sid in list(self.storage.sessions.keys()):
            try:
                self.storage.destroy(sid)
            except Exception:
                pass

        # Stop cleanup thread if running
        self.storage.stop_cleanup_thread()

        # Safety net: force-kill any tracked PIDs still alive
        for pid in self._tracked_pids:
            if is_process_alive(pid):
                logging.warning(f"tearDown: force-killing orphaned PID {pid}")
                force_kill_pid(pid)

    def _get_chrome_pid(self, session) -> int:
        """Extract Chrome PID from session via CDP port lookup, with fallback."""
        pid = self.storage._find_pid_by_port(session.cdp_port)
        if pid:
            self._tracked_pids.append(pid)
            return pid
        # Fallback: try browser_pid attribute (undetected_chromedriver)
        if hasattr(session.driver, 'browser_pid') and session.driver.browser_pid:
            self._tracked_pids.append(session.driver.browser_pid)
            return session.driver.browser_pid
        self.fail("Could not determine Chrome PID")

    def test_destroy_kills_chrome_process(self):
        """Happy path: create session, destroy it, verify Chrome PID is dead."""
        session, created = self.storage.create("integration-test-destroy")
        self.assertTrue(created)

        chrome_pid = self._get_chrome_pid(session)
        self.assertTrue(is_process_alive(chrome_pid),
                        f"Chrome PID {chrome_pid} should be alive after create")

        self.storage.destroy("integration-test-destroy")
        time.sleep(1)

        self.assertFalse(is_process_alive(chrome_pid),
                         f"Chrome PID {chrome_pid} should be dead after destroy")

    def test_force_kill_when_graceful_quit_fails(self):
        """When driver.quit/close raise, the force-kill path should still clean up."""
        session, created = self.storage.create("integration-test-forcekill")
        self.assertTrue(created)

        chrome_pid = self._get_chrome_pid(session)
        self.assertTrue(is_process_alive(chrome_pid))

        # Monkeypatch driver.quit and driver.close to raise
        def raise_quit():
            raise Exception("Simulated quit failure")

        def raise_close():
            raise Exception("Simulated close failure")

        session.driver.quit = raise_quit
        session.driver.close = raise_close

        self.storage.destroy("integration-test-forcekill")
        time.sleep(1)

        self.assertFalse(is_process_alive(chrome_pid),
                         f"Chrome PID {chrome_pid} should be dead even when graceful quit fails")

    def test_no_zombie_accumulation_across_cycles(self):
        """Create/destroy 3 times — no zombie Chrome processes should accumulate."""
        collected_pids = []

        for i in range(3):
            session_id = f"integration-test-cycle-{i}"
            session, created = self.storage.create(session_id)
            self.assertTrue(created)

            chrome_pid = self._get_chrome_pid(session)
            collected_pids.append(chrome_pid)
            self.assertTrue(is_process_alive(chrome_pid))

            self.storage.destroy(session_id)
            time.sleep(1)

            self.assertFalse(is_process_alive(chrome_pid),
                             f"Cycle {i}: Chrome PID {chrome_pid} should be dead after destroy")

        # Final sweep: ALL collected PIDs must be dead
        for pid in collected_pids:
            self.assertFalse(is_process_alive(pid),
                             f"Final sweep: Chrome PID {pid} is still alive (zombie)")

    def test_idle_timeout_kills_chrome_process(self):
        """Session with idle_minutes should be cleaned up by cleanup_expired_sessions."""
        session, created = self.storage.create(
            "integration-test-idle",
            idle_minutes=1
        )
        self.assertTrue(created)

        chrome_pid = self._get_chrome_pid(session)
        self.assertTrue(is_process_alive(chrome_pid))

        # Backdate last_activity by 2 minutes to trigger expiry
        session.last_activity = datetime.now() - timedelta(minutes=2)

        # Call cleanup directly (not via background thread)
        cleaned = self.storage.cleanup_expired_sessions()
        self.assertEqual(cleaned, 1, "Should have cleaned up 1 expired session")

        time.sleep(1)

        self.assertFalse(is_process_alive(chrome_pid),
                         f"Chrome PID {chrome_pid} should be dead after idle timeout cleanup")

    def test_failed_creation_no_orphan_processes(self):
        """When create() fails after Chrome starts, Chrome should be cleaned up."""
        captured_pid = {}
        original_get_webdriver = utils.get_webdriver

        def patched_get_webdriver(*args, **kwargs):
            driver = original_get_webdriver(*args, **kwargs)
            # Capture PID before raising
            if hasattr(driver, 'browser_pid') and driver.browser_pid:
                captured_pid['pid'] = driver.browser_pid
                self._tracked_pids.append(driver.browser_pid)
            raise RuntimeError("Simulated failure after Chrome start")

        with patch('sessions.utils.get_webdriver', side_effect=patched_get_webdriver):
            with self.assertRaises(RuntimeError):
                self.storage.create("integration-test-failed-create")

        self.assertIn('pid', captured_pid,
                      "Should have captured Chrome PID before failure")
        chrome_pid = captured_pid['pid']

        time.sleep(1)

        # This assertion exposes the bug: Chrome is still alive because
        # create() has no try-except around get_webdriver()
        self.assertFalse(is_process_alive(chrome_pid),
                         f"Chrome PID {chrome_pid} should be dead after failed create, "
                         f"but it's orphaned (bug: no cleanup on create failure)")


if __name__ == '__main__':
    unittest.main(verbosity=2)
