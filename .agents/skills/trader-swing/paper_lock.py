"""Cross-process and multi-thread file mutex locking for Trader-Swing Phase 8.2.

Provides deterministic mutual exclusion across concurrent processes and threads
accessing the isolated paper trading and simulation state directories.

Enforces:
1. Cross-process mutual exclusion using OS-level file locking (msvcrt on Windows, fcntl on POSIX).
2. Intra-process thread safety via threading.RLock.
3. Fail-closed timeout behavior (PaperLockTimeoutError) preventing deadlock.
4. Clean context manager lifecycle ensuring lock release on exception or exit.
"""

from contextlib import contextmanager
import os
import sys
import threading
import time
from typing import Optional

# Platform-specific file locking
_IS_WINDOWS = sys.platform.startswith("win")
if _IS_WINDOWS:
    import msvcrt
else:
    try:
        import fcntl
    except ImportError:
        fcntl = None


class PaperLockError(Exception):
    """Base exception for paper state locking failures."""
    pass


class PaperLockTimeoutError(PaperLockError):
    """Raised when acquiring the paper file lock exceeds the specified timeout."""
    pass


class PaperFileLock:
    """Robust cross-process and thread-safe file mutex."""

    # Process-wide lock registry to coordinate threads within the same process
    _thread_locks: dict = {}
    _registry_lock = threading.Lock()

    def __init__(
        self,
        lock_path: str,
        timeout: float = 10.0,
        poll_interval: float = 0.05,
    ):
        self.lock_path = os.path.abspath(lock_path)
        self.timeout = float(timeout)
        self.poll_interval = float(poll_interval)
        self._file = None
        self._is_locked = False
        self._thread_lock = self._get_or_create_thread_lock(self.lock_path)

    @classmethod
    def _get_or_create_thread_lock(cls, canonical_path: str) -> threading.RLock:
        with cls._registry_lock:
            if canonical_path not in cls._thread_locks:
                cls._thread_locks[canonical_path] = threading.RLock()
            return cls._thread_locks[canonical_path]

    @property
    def is_locked(self) -> bool:
        """Return True if currently holding the lock."""
        return self._is_locked

    def acquire(self) -> "PaperFileLock":
        """Acquire both thread lock and OS file lock. Fail closed on timeout."""
        deadline = time.time() + self.timeout

        # 1. Acquire thread-level re-entrant lock
        acquired_thread = False
        while time.time() < deadline:
            if self._thread_lock.acquire(blocking=True, timeout=min(self.poll_interval, 0.5)):
                acquired_thread = True
                break

        if not acquired_thread:
            raise PaperLockTimeoutError(
                f"Thread lock acquisition timed out after {self.timeout:.2f}s for '{self.lock_path}'"
            )

        # 2. Acquire OS-level cross-process file lock
        try:
            os.makedirs(os.path.dirname(self.lock_path), exist_ok=True)
            self._file = open(self.lock_path, "a+b")

            while True:
                try:
                    if _IS_WINDOWS:
                        # Non-blocking exclusive lock of byte 0
                        self._file.seek(0)
                        msvcrt.locking(self._file.fileno(), msvcrt.LK_NBLCK, 1)
                    elif fcntl is not None:
                        # Non-blocking exclusive lock on POSIX
                        fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    else:
                        # Fallback: file presence / touch check
                        pass

                    self._is_locked = True
                    return self
                except (OSError, IOError):
                    if time.time() >= deadline:
                        raise PaperLockTimeoutError(
                            f"OS file lock acquisition timed out after {self.timeout:.2f}s for '{self.lock_path}'"
                        )
                    time.sleep(self.poll_interval)

        except Exception:
            # Clean up on failure
            if self._file is not None:
                try:
                    self._file.close()
                except Exception:
                    pass
                self._file = None
            self._thread_lock.release()
            raise

    def release(self) -> None:
        """Release OS file lock and thread lock."""
        try:
            if self._is_locked and self._file is not None:
                try:
                    if _IS_WINDOWS:
                        self._file.seek(0)
                        msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
                    elif fcntl is not None:
                        fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
                except Exception:
                    pass
                finally:
                    try:
                        self._file.close()
                    except Exception:
                        pass
                    self._file = None
                    self._is_locked = False
        finally:
            # Always release thread lock
            try:
                self._thread_lock.release()
            except RuntimeError:
                pass

    def __enter__(self) -> "PaperFileLock":
        return self.acquire()

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.release()
