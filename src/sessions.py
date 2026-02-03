import logging
import os
import signal
import socket
import subprocess
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional, Tuple
from uuid import uuid1

from selenium.webdriver.chrome.webdriver import WebDriver

import utils


def find_free_port() -> int:
    """Find a free port on localhost"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('', 0))
        s.listen(1)
        port = s.getsockname()[1]
    return port


@dataclass
class Session:
    session_id: str
    driver: WebDriver
    created_at: datetime
    cdp_port: int
    cdp_url: str
    unhealthy: bool = field(default=False)  # Mark unhealthy on timeout
    idle_minutes: Optional[int] = field(default=None)  # Auto-destroy after this duration of inactivity
    last_activity: datetime = field(default_factory=datetime.now)  # Track last activity for inactivity-based cleanup

    def lifetime(self) -> timedelta:
        return datetime.now() - self.created_at

    def time_since_last_activity(self) -> timedelta:
        return datetime.now() - self.last_activity

    def is_expired(self) -> bool:
        """Check if session has exceeded its idle timeout."""
        if self.idle_minutes is None:
            return False
        return self.time_since_last_activity() > timedelta(minutes=self.idle_minutes)

    def touch(self):
        """Update last activity timestamp to reset inactivity timer."""
        self.last_activity = datetime.now()


class SessionsStorage:
    """SessionsStorage creates, stores and process all the sessions"""

    def __init__(self):
        self.sessions = {}
        self._lock = threading.Lock()
        self._cleanup_thread: Optional[threading.Thread] = None
        self._cleanup_stop_event = threading.Event()
        self._cleanup_interval_seconds = 60  # Check every minute by default

    def create(self, session_id: Optional[str] = None, proxy: Optional[dict] = None,
               force_new: Optional[bool] = False,
               idle_minutes: Optional[int] = None) -> Tuple[Session, bool]:
        """create creates new instance of WebDriver if necessary,
        assign defined (or newly generated) session_id to the instance
        and returns the session object. If a new session has been created
        second argument is set to True.

        Note: The function is idempotent, so in case if session_id
        already exists in the storage a new instance of WebDriver won't be created
        and existing session will be returned. Second argument defines if
        new session has been created (True) or an existing one was used (False).

        If idle_minutes is provided, the session will be automatically
        destroyed by the cleanup thread after this duration of inactivity.
        """
        session_id = session_id or str(uuid1())

        if force_new:
            self.destroy(session_id)

        with self._lock:
            # Check directly - don't call exists() as that would deadlock
            if session_id in self.sessions:
                existing_session = self.sessions[session_id]
                # Update idle_minutes if provided and session exists
                if idle_minutes is not None:
                    existing_session.idle_minutes = idle_minutes
                return existing_session, False

            env_cdp_port = os.environ.get('CDP_PORT')
            if env_cdp_port:
                cdp_port = int(env_cdp_port)
                logging.info(f"Using CDP_PORT from environment: {cdp_port}")
            else:
                cdp_port = find_free_port()
                logging.info(f"Allocated dynamic CDP port: {cdp_port}")

            cdp_url = f'http://localhost:{cdp_port}'

            driver = utils.get_webdriver(proxy, cdp_port=cdp_port)
            created_at = datetime.now()

            session = Session(session_id, driver, created_at, cdp_port, cdp_url,
                              idle_minutes=idle_minutes)

            if idle_minutes is not None:
                logging.info(f"Session {session_id} created with idle_minutes={idle_minutes}")

            self.sessions[session_id] = session

            return session, True

    def exists(self, session_id: str) -> bool:
        with self._lock:
            return session_id in self.sessions

    def destroy(self, session_id: str) -> bool:
        """destroy closes the driver instance and removes session from the storage.
        The function is noop if session_id doesn't exist.
        The function returns True if session was found and destroyed,
        and False if session_id wasn't found.
        """
        # Pop session from storage while holding lock
        with self._lock:
            if session_id not in self.sessions:
                return False
            session = self.sessions.pop(session_id)

        # Perform cleanup OUTSIDE lock (slow operation)
        self._cleanup_driver(session.driver, session.cdp_port)
        return True

    def _find_pid_by_port(self, port: int) -> Optional[int]:
        """Find the process ID that is listening on the given port."""
        try:
            if os.name == 'nt':
                # Windows: use netstat to find the PID
                result = subprocess.run(
                    ['netstat', '-ano'],
                    capture_output=True,
                    text=True,
                    timeout=10
                )
                for line in result.stdout.splitlines():
                    # Look for LISTENING on the specific port
                    if f':{port}' in line and 'LISTENING' in line:
                        parts = line.split()
                        if len(parts) >= 5:
                            pid = int(parts[-1])
                            logging.debug(f"Found PID {pid} listening on port {port}")
                            return pid
            else:
                # Linux/Mac: use lsof
                result = subprocess.run(
                    ['lsof', '-i', f':{port}', '-t'],
                    capture_output=True,
                    text=True,
                    timeout=10
                )
                if result.stdout.strip():
                    pid = int(result.stdout.strip().split('\n')[0])
                    logging.debug(f"Found PID {pid} listening on port {port}")
                    return pid
        except Exception as e:
            logging.warning(f"Failed to find PID by port {port}: {e}")
        return None

    def _cleanup_driver(self, driver: WebDriver, cdp_port: Optional[int] = None):
        """Clean up a WebDriver instance, using force-kill by CDP port if graceful quit fails."""
        # Find Chrome PID BEFORE quit (it may release port after quit but still be running)
        chrome_pid = None
        if cdp_port:
            chrome_pid = self._find_pid_by_port(cdp_port)
            if chrome_pid:
                logging.debug(f"Found Chrome PID {chrome_pid} on CDP port {cdp_port} before cleanup")

        # Try graceful quit first
        graceful_success = False
        try:
            if utils.PLATFORM_VERSION == "nt":
                driver.close()
            driver.quit()
            logging.debug("Driver quit gracefully")
            graceful_success = True
        except Exception as e:
            logging.warning(f"Graceful quit failed: {e}")

        # On Windows, always verify Chrome is dead and force-kill if needed
        if os.name == 'nt' and chrome_pid:
            # Check if Chrome is still running
            try:
                result = subprocess.run(
                    ['tasklist', '/FI', f'PID eq {chrome_pid}'],
                    capture_output=True,
                    text=True,
                    timeout=5
                )
                if str(chrome_pid) in result.stdout:
                    logging.debug(f"Chrome PID {chrome_pid} still running after quit, force-killing")
                    subprocess.run(
                        ['taskkill', '/F', '/T', '/PID', str(chrome_pid)],
                        timeout=10,
                        capture_output=True
                    )
                    logging.info(f"Force-killed Chrome process PID {chrome_pid} (CDP port {cdp_port})")
            except Exception as e:
                logging.warning(f"Post-quit cleanup check failed: {e}")
        elif not graceful_success and chrome_pid:
            # Non-Windows: force kill if graceful failed
            try:
                os.kill(chrome_pid, signal.SIGKILL)
                logging.info(f"Force-killed Chrome process PID {chrome_pid} (CDP port {cdp_port})")
            except Exception as e:
                logging.warning(f"Force kill by PID {chrome_pid} failed: {e}")

    def get(self, session_id: str, ttl: Optional[timedelta] = None) -> Tuple[Session, bool]:
        session, fresh = self.create(session_id)

        # Recreate if TTL expired or marked unhealthy
        should_recreate = False
        if not fresh:
            if session.unhealthy:
                logging.debug(f'session is unhealthy, recreating (session_id={session_id})')
                should_recreate = True
            elif ttl is not None and session.lifetime() > ttl:
                logging.debug(f'session\'s lifetime has expired, so the session is recreated (session_id={session_id})')
                should_recreate = True

        if should_recreate:
            session, fresh = self.create(session_id, force_new=True)

        return session, fresh

    def session_ids(self) -> list[str]:
        with self._lock:
            return list(self.sessions.keys())

    def cleanup_expired_sessions(self) -> int:
        """Check all sessions and destroy those that have exceeded their max lifetime.
        Returns the number of sessions cleaned up.
        """
        sessions_to_destroy = []

        # Collect expired sessions while holding lock briefly
        with self._lock:
            for session_id, session in self.sessions.items():
                if session.is_expired():
                    sessions_to_destroy.append((session_id, session.time_since_last_activity(), session.idle_minutes))

        # Destroy sessions outside lock (slow operation)
        cleaned_up = 0
        for session_id, inactivity, idle_mins in sessions_to_destroy:
            logging.info(f"Auto-destroying idle session {session_id} "
                         f"(idle for {inactivity}, idle_minutes={idle_mins})")
            try:
                self.destroy(session_id)
                cleaned_up += 1
            except Exception as e:
                logging.warning(f"Failed to destroy expired session {session_id}: {e}")

        return cleaned_up

    def _cleanup_loop(self):
        """Background thread loop that periodically cleans up expired sessions."""
        logging.info(f"Session cleanup thread started (interval={self._cleanup_interval_seconds}s)")
        while not self._cleanup_stop_event.is_set():
            try:
                cleaned = self.cleanup_expired_sessions()
                if cleaned > 0:
                    logging.info(f"Cleanup thread destroyed {cleaned} expired session(s)")
            except Exception as e:
                logging.error(f"Error in cleanup thread: {e}")

            # Wait for interval or stop event
            self._cleanup_stop_event.wait(self._cleanup_interval_seconds)

        logging.info("Session cleanup thread stopped")

    def start_cleanup_thread(self, interval_seconds: int = 60):
        """Start the background cleanup thread.

        Args:
            interval_seconds: How often to check for expired sessions (default: 60)
        """
        if self._cleanup_thread is not None and self._cleanup_thread.is_alive():
            logging.warning("Cleanup thread already running")
            return

        self._cleanup_interval_seconds = interval_seconds
        self._cleanup_stop_event.clear()
        self._cleanup_thread = threading.Thread(target=self._cleanup_loop, daemon=True)
        self._cleanup_thread.start()

    def stop_cleanup_thread(self):
        """Stop the background cleanup thread."""
        if self._cleanup_thread is None or not self._cleanup_thread.is_alive():
            return

        self._cleanup_stop_event.set()
        self._cleanup_thread.join(timeout=5)
        self._cleanup_thread = None
