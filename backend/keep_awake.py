"""Cross-platform helper to prevent the OS from sleeping while the worker
is running.

On Windows this uses `SetThreadExecutionState(ES_CONTINUOUS |
ES_SYSTEM_REQUIRED | ES_AWAYMODE_REQUIRED)` to ask the system to stay
awake even on Modern Standby (S0 Low-Power Idle) machines, which is what
most current laptops use. The display is allowed to turn off; only system
sleep / suspension is suppressed. On non-Windows platforms this is a
no-op.

`ES_AWAYMODE_REQUIRED` matters because on Modern Standby laptops the
classic `ES_SYSTEM_REQUIRED` alone is *not* enough — the OS will still
suspend background processes once the user is idle. Adding
`ES_AWAYMODE_REQUIRED` tells Windows the app is doing legitimate
background work and the system should remain in S0 instead of dropping
to connected standby. Group-policy / power-policy settings can still
override this; see README.

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
import subprocess
import sys
import threading
from typing import Optional

log = logging.getLogger("docregistrar.keep_awake")

# Windows constants for SetThreadExecutionState. Only ES_CONTINUOUS and
# ES_SYSTEM_REQUIRED are needed for *classic* system-stay-awake;
# ES_DISPLAY_REQUIRED would also keep the display on (we deliberately do
# NOT request it). ES_AWAYMODE_REQUIRED additionally protects Modern
# Standby laptops from suspending the worker while it's doing background
# LLM work.
_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001
_ES_AWAYMODE_REQUIRED = 0x00000040


def _detect_modern_standby() -> Optional[bool]:
    """Best-effort check for whether the current system uses Modern Standby
    (S0 Low-Power Idle) or classic S3 sleep. Returns True / False / None
    (None on platforms or environments where we can't tell).

    Implementation: parse `powercfg /a` once. The string "S0 Low Power
    Idle" indicates Modern Standby.
    """
    if not sys.platform.startswith("win"):
        return None
    try:
        result = subprocess.run(
            ["powercfg", "/a"],
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
            # Don't pop a console window when the parent process is GUI.
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        out = (result.stdout or "") + "\n" + (result.stderr or "")
    except Exception:
        return None
    if not out:
        return None
    # Locale-independent enough: the literal "S0" prefix appears on
    # English systems; for non-English we still match the bracketed
    # token pattern Windows uses for sleep states.
    return ("S0 Low Power Idle" in out) or ("S0 " in out)


class KeepAwake:
    """Idempotent OS-sleep suppressor for the worker thread."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._held = False
        self._previous_state: Optional[int] = None
        self._supported = sys.platform.startswith("win")
        # Cached on first detect; None until we ask Windows.
        self._modern_standby: Optional[bool] = None
        # Last status string produced by acquire(); used by callers that
        # want to surface the keep-awake state in the activity log.
        self._last_status: str = ""

    @property
    def supported(self) -> bool:
        """True if this platform actually has a working implementation."""
        return self._supported

    @property
    def is_held(self) -> bool:
        with self._lock:
            return self._held

    @property
    def modern_standby(self) -> Optional[bool]:
        """Whether Windows is using Modern Standby (S0). None if unknown
        / non-Windows. Computed on first acquire(), then cached."""
        return self._modern_standby

    @property
    def last_status(self) -> str:
        """Human-readable summary of the most recent acquire() outcome.
        Empty string if acquire() hasn't been called yet."""
        return self._last_status

    def acquire(self) -> bool:
        """Request the OS to stay awake. Returns True if successful (or
        already held), False if the platform isn't supported or the call
        failed.

        On Windows, we OR in `ES_AWAYMODE_REQUIRED` so that Modern Standby
        machines also stay awake while the worker is running. The classic
        `ES_SYSTEM_REQUIRED` flag alone is not enough on those laptops:
        Windows will still suspend background processes once the user is
        idle, which causes our heartbeat to silently freeze for minutes
        at a time.
        """
        with self._lock:
            if self._held:
                return True
            if not self._supported:
                # Non-Windows: no-op. Mark as "held" so release() is balanced.
                self._held = True
                self._last_status = (
                    f"keep-awake not supported on {sys.platform}; system "
                    "sleep policy unchanged"
                )
                return False
            # Detect Modern Standby once. Cheap (one ~200ms subprocess on
            # cold start, then cached); guarded by the same lock so we
            # don't race two acquires.
            if self._modern_standby is None:
                self._modern_standby = _detect_modern_standby()
            try:
                import ctypes
                kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
                # SetThreadExecutionState returns the PREVIOUS state on
                # success, 0 on failure (NULL).
                flags = _ES_CONTINUOUS | _ES_SYSTEM_REQUIRED | _ES_AWAYMODE_REQUIRED
                prev = kernel32.SetThreadExecutionState(flags)
                if prev == 0:
                    self._last_status = (
                        "SetThreadExecutionState returned 0; keep-awake "
                        "may not be active"
                    )
                    log.warning(self._last_status)
                    return False
                self._previous_state = prev
                self._held = True
                ms_label = (
                    "Modern Standby (S0)" if self._modern_standby is True
                    else "classic sleep (S3)" if self._modern_standby is False
                    else "unknown power model"
                )
                self._last_status = (
                    f"keep-awake acquired "
                    f"(ES_CONTINUOUS|ES_SYSTEM_REQUIRED|ES_AWAYMODE_REQUIRED); "
                    f"power model: {ms_label}"
                )
                log.info(self._last_status)
                if self._modern_standby is True:
                    log.info(
                        "On Modern Standby laptops, Windows can still "
                        "suspend background work if Group Policy / power "
                        "policy overrides our hint. If the worker keeps "
                        "stalling, set Settings > System > Power > Sleep > "
                        "'Plugged in' to Never, or run with the lid open "
                        "and AC connected."
                    )
                return True
            except Exception as e:
                self._last_status = f"could not acquire keep-awake: {e}"
                log.warning(self._last_status)
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