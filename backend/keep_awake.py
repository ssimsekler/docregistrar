"""Cross-platform helper to prevent the OS from sleeping while the worker
is running.

On Windows this uses `SetThreadExecutionState(ES_CONTINUOUS |
ES_SYSTEM_REQUIRED)` to ask the system to stay awake. The display is
allowed to turn off; only system sleep is suppressed. On non-Windows
platforms this is a no-op.

Usage:
    awake = KeepAwake()
    awake.acquire()         # tells the OS "stay awake"
    ...do work...
    awake.release()         # restores normal sleep policy

`acquire`/`release` are idempotent. The class also implements the
context-manager protocol.
"""
from __future__ import annotations

import logging
import sys
import threading
from typing import Optional

log = logging.getLogger("docregistrar.keep_awake")

# Windows constants for SetThreadExecutionState. Only ES_CONTINUOUS and
# ES_SYSTEM_REQUIRED are needed for system-stay-awake; ES_DISPLAY_REQUIRED
# would also keep the display on, which we deliberately do NOT request.
_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001


class KeepAwake:
    """Idempotent OS-sleep suppressor for the worker thread."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._held = False
        self._previous_state: Optional[int] = None
        self._supported = sys.platform.startswith("win")

    @property
    def supported(self) -> bool:
        """True if this platform actually has a working implementation."""
        return self._supported

    @property
    def is_held(self) -> bool:
        with self._lock:
            return self._held

    def acquire(self) -> bool:
        """Request the OS to stay awake. Returns True if successful (or
        already held), False if the platform isn't supported or the call
        failed."""
        with self._lock:
            if self._held:
                return True
            if not self._supported:
                # Non-Windows: no-op. Mark as "held" so release() is balanced.
                self._held = True
                return False
            try:
                import ctypes
                kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
                # SetThreadExecutionState returns the PREVIOUS state on
                # success, 0 on failure (NULL).
                prev = kernel32.SetThreadExecutionState(
                    _ES_CONTINUOUS | _ES_SYSTEM_REQUIRED
                )
                if prev == 0:
                    log.warning(
                        "SetThreadExecutionState returned 0; keep-awake "
                        "may not be active."
                    )
                    return False
                self._previous_state = prev
                self._held = True
                log.info(
                    "Keep-awake acquired (system sleep suppressed; "
                    "display may still turn off)."
                )
                return True
            except Exception as e:
                log.warning("Could not acquire keep-awake: %s", e)
                return False

    def release(self) -> None:
        """Restore normal sleep policy. Safe to call multiple times."""
        with self._lock:
            if not self._held:
                return
            self._held = False
            if not self._supported:
                return
            try:
                import ctypes
                kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
                # Clearing ES_SYSTEM_REQUIRED by passing only ES_CONTINUOUS
                # restores the default. Some docs suggest passing the
                # previous state, but ES_CONTINUOUS alone is the canonical
                # "release" call.
                kernel32.SetThreadExecutionState(_ES_CONTINUOUS)
                log.info("Keep-awake released; normal sleep policy restored.")
            except Exception as e:
                log.warning("Could not release keep-awake: %s", e)

    def __enter__(self) -> "KeepAwake":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()