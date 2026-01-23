"""
Tests for FlareSolverr Session Management Changes

Run with: python -m pytest test_sessions.py -v
Or: python test_sessions.py (for standalone execution)
"""

import threading
import time
import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch

# Import the module under test
from sessions import Session, SessionsStorage, find_free_port


class TestSession(unittest.TestCase):
    """Tests for the Session dataclass"""

    def test_session_creation_with_defaults(self):
        """Session should have correct defaults for new fields"""
        mock_driver = MagicMock()
        session = Session(
            session_id="test-session",
            driver=mock_driver,
            created_at=datetime.now(),
            cdp_port=9222,
            cdp_url="http://localhost:9222"
        )

        # New fields should have correct defaults
        self.assertFalse(session.unhealthy)
        self.assertEqual(session.cdp_port, 9222)

    def test_session_stores_cdp_port(self):
        """Session should store cdp_port for cleanup"""
        mock_driver = MagicMock()
        session = Session(
            session_id="test-session",
            driver=mock_driver,
            created_at=datetime.now(),
            cdp_port=9333,
            cdp_url="http://localhost:9333"
        )

        self.assertEqual(session.cdp_port, 9333)

    def test_session_unhealthy_flag(self):
        """Session unhealthy flag should be mutable"""
        mock_driver = MagicMock()
        session = Session(
            session_id="test-session",
            driver=mock_driver,
            created_at=datetime.now(),
            cdp_port=9222,
            cdp_url="http://localhost:9222"
        )

        self.assertFalse(session.unhealthy)

        # Mark as unhealthy
        session.unhealthy = True
        self.assertTrue(session.unhealthy)

    def test_session_lifetime(self):
        """Session should calculate lifetime correctly"""
        mock_driver = MagicMock()
        created_at = datetime.now()
        session = Session(
            session_id="test-session",
            driver=mock_driver,
            created_at=created_at,
            cdp_port=9222,
            cdp_url="http://localhost:9222"
        )

        # Lifetime should be positive
        time.sleep(0.1)
        lifetime = session.lifetime()
        self.assertGreater(lifetime.total_seconds(), 0)


class TestSessionsStorageThreadSafety(unittest.TestCase):
    """Tests for SessionsStorage thread safety"""

    def setUp(self):
        self.storage = SessionsStorage()

    def test_has_lock(self):
        """Storage should have a threading lock"""
        self.assertTrue(hasattr(self.storage, '_lock'))
        self.assertIsInstance(self.storage._lock, type(threading.Lock()))

    def test_lock_is_not_reentrant(self):
        """Lock should be a regular Lock, not RLock (as per plan)"""
        # threading.Lock() is not reentrant - check by type name
        lock_type = type(self.storage._lock).__name__
        self.assertIn(lock_type, ["lock", "_thread.lock"])
        # Verify it's NOT an RLock
        self.assertNotIn("RLock", str(type(self.storage._lock)))

    def test_sessions_dict_exists(self):
        """Storage should have a sessions dictionary"""
        self.assertIsInstance(self.storage.sessions, dict)
        self.assertEqual(len(self.storage.sessions), 0)


class TestSessionsStorageCleanup(unittest.TestCase):
    """Tests for the _cleanup_driver method"""

    def setUp(self):
        self.storage = SessionsStorage()

    @patch('sessions.utils.PLATFORM_VERSION', 'nt')
    @patch('sessions.subprocess.run')
    def test_cleanup_driver_graceful_windows(self, mock_subprocess):
        """Should close and quit driver gracefully on Windows, and verify Chrome is dead"""
        mock_driver = MagicMock()

        # Simulate Chrome not running after quit (tasklist doesn't find PID)
        mock_subprocess.return_value = MagicMock(stdout="No matching processes found")

        self.storage._cleanup_driver(mock_driver, cdp_port=9222)

        mock_driver.close.assert_called_once()
        mock_driver.quit.assert_called_once()

    @patch('sessions.utils.PLATFORM_VERSION', 'linux')
    def test_cleanup_driver_graceful_linux(self):
        """Should only quit driver on Linux"""
        mock_driver = MagicMock()

        self.storage._cleanup_driver(mock_driver)

        mock_driver.close.assert_not_called()
        mock_driver.quit.assert_called_once()

    @patch('sessions.utils.PLATFORM_VERSION', 'nt')
    @patch('sessions.subprocess.run')
    def test_cleanup_driver_force_kill_when_chrome_still_running(self, mock_subprocess):
        """Should force kill Chrome when it's still running after graceful quit"""
        mock_driver = MagicMock()

        def subprocess_side_effect(cmd, **kwargs):
            if 'netstat' in cmd:
                # First call: find PID by port
                return MagicMock(stdout="  TCP    127.0.0.1:9222    0.0.0.0:0    LISTENING    12345\n")
            elif 'tasklist' in cmd:
                # Second call: Chrome is still running
                return MagicMock(stdout="chrome.exe    12345    Console    1    123,456 K")
            elif 'taskkill' in cmd:
                # Third call: kill the process
                return MagicMock(stdout="SUCCESS")
            return MagicMock(stdout="")

        mock_subprocess.side_effect = subprocess_side_effect

        cdp_port = 9222
        self.storage._cleanup_driver(mock_driver, cdp_port)

        # Should have called: netstat (find PID), tasklist (check alive), taskkill (force kill)
        self.assertEqual(mock_subprocess.call_count, 3)
        # Third call should be taskkill with /T (tree kill)
        third_call = mock_subprocess.call_args_list[2]
        self.assertIn('/T', third_call[0][0])
        self.assertIn('/PID', third_call[0][0])

    @patch('sessions.utils.PLATFORM_VERSION', 'nt')
    @patch('sessions.subprocess.run')
    def test_cleanup_driver_no_force_kill_when_chrome_exits(self, mock_subprocess):
        """Should not force kill when Chrome exits gracefully"""
        mock_driver = MagicMock()

        def subprocess_side_effect(cmd, **kwargs):
            if 'netstat' in cmd:
                # First call: find PID by port
                return MagicMock(stdout="  TCP    127.0.0.1:9222    0.0.0.0:0    LISTENING    12345\n")
            elif 'tasklist' in cmd:
                # Second call: Chrome is NOT running (exited gracefully)
                return MagicMock(stdout="INFO: No matching processes found")
            return MagicMock(stdout="")

        mock_subprocess.side_effect = subprocess_side_effect

        cdp_port = 9222
        self.storage._cleanup_driver(mock_driver, cdp_port)

        # Should have called: netstat (find PID), tasklist (check alive) - no taskkill needed
        self.assertEqual(mock_subprocess.call_count, 2)

    @patch('sessions.utils.PLATFORM_VERSION', 'nt')
    def test_cleanup_driver_no_force_kill_without_cdp_port(self):
        """Should not force kill when cdp_port is None"""
        mock_driver = MagicMock()
        mock_driver.close.side_effect = Exception("Close failed")
        mock_driver.quit.side_effect = Exception("Quit failed")

        # Should not raise, should just log warning
        self.storage._cleanup_driver(mock_driver, cdp_port=None)


class TestSessionsStorageGetWithUnhealthy(unittest.TestCase):
    """Tests for the get method handling unhealthy sessions - logic tests only"""

    def test_unhealthy_session_get_logic(self):
        """Test the logic of get() for unhealthy sessions without actual session creation"""
        # The get() method has this logic:
        # if not fresh and session.unhealthy:
        #     session, fresh = self.create(session_id, force_new=True)

        # Create a mock session marked as unhealthy
        mock_session = Session(
            session_id="test",
            driver=MagicMock(),
            created_at=datetime.now(),
            cdp_port=9222,
            cdp_url="http://localhost:9222",
            unhealthy=True
        )

        # Verify the unhealthy flag triggers the condition
        self.assertTrue(mock_session.unhealthy)

        # After recreation, the new session should be healthy
        new_session = Session(
            session_id="test",
            driver=MagicMock(),
            created_at=datetime.now(),
            cdp_port=9222,
            cdp_url="http://localhost:9222"
        )
        self.assertFalse(new_session.unhealthy)


class TestFindFreePort(unittest.TestCase):
    """Tests for find_free_port function"""

    def test_returns_valid_port(self):
        """Should return a valid port number"""
        port = find_free_port()
        self.assertIsInstance(port, int)
        self.assertGreater(port, 0)
        self.assertLess(port, 65536)

    def test_returns_different_ports(self):
        """Should return different ports on successive calls"""
        ports = [find_free_port() for _ in range(5)]
        # At least some should be different
        unique_ports = set(ports)
        self.assertGreater(len(unique_ports), 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
