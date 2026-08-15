"""Game profile abstraction.

A `GameProfile` describes everything OpenFOV needs to know about one
target game:
- its process name(s) (for auto-detection)
- the GameID + encryption key it expects to see in FT_SharedMem
- the default per-axis settings appropriate for that game
- a display label and id slug for the UI

A `GameDetector` polls `psutil.process_iter()` on a configurable interval
and emits callbacks when the active game changes. v1 ships a single
multiplexing detector that handles all registered profiles in one pass."""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from threading import Event, Thread

from openfov.mapping.axis_mapper import AxisSettings
from openfov.output.manager import GameOutputProfile

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GameProfile:
    """One target game's static identity + defaults."""

    id: str                                                   # slug, e.g. "iracing"
    display_name: str                                         # "iRacing"
    process_names: tuple[str, ...]                            # ("iRacingSim64DX11.exe", ...)
    output: GameOutputProfile = field(default_factory=GameOutputProfile)
    default_axes: dict[str, AxisSettings] | None = None       # None = use profile defaults

    # Whether this game needs a live process named TrackIR.exe before it
    # will initialize head tracking. Falcon BMS and parts of MSFS do; most
    # titles (iRacing included) locate us purely through the NPClient
    # registry key and never look at the process list.
    #
    # Default False on purpose. The bundled helper attracts antivirus false
    # positives — Defender flagged the 0.2.1 build as
    # Trojan:Win32/Ravartar!rfn — so we only launch it for games that
    # actually require it rather than unconditionally.
    requires_trackir_process: bool = False


class GameDetector:
    """Polls running processes and reports which registered GameProfile is
    currently active. Calls `on_change(active_profile | None)` whenever the
    state flips. Runs on its own daemon thread."""

    def __init__(
        self,
        profiles: list[GameProfile],
        on_change: Callable[[GameProfile | None], None],
        poll_interval_s: float = 1.0,
    ) -> None:
        self._profiles = profiles
        self._on_change = on_change
        self._poll_interval = poll_interval_s
        self._stop_evt = Event()
        self._thread: Thread | None = None
        self._current: GameProfile | None = None

        # Build a fast lookup: lowercased process name -> profile.
        self._proc_to_profile: dict[str, GameProfile] = {}
        for profile in profiles:
            for name in profile.process_names:
                self._proc_to_profile[name.lower()] = profile

    @property
    def current(self) -> GameProfile | None:
        return self._current

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_evt.clear()
        self._thread = Thread(target=self._run, name="openfov-gamedetect", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_evt.set()
        if self._thread is not None:
            self._thread.join(timeout=self._poll_interval * 2)
            self._thread = None

    def poll_once(self) -> GameProfile | None:
        """Single synchronous scan. Returns the active profile (or None).
        Public for testing — the detector thread also calls this internally."""
        if sys.platform != "win32":
            return None
        try:
            import psutil
        except ImportError:
            return None

        for proc in psutil.process_iter(["name"]):
            try:
                name = proc.info.get("name")
                if not name:
                    continue
                profile = self._proc_to_profile.get(name.lower())
                if profile is not None:
                    return profile
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return None

    def _run(self) -> None:
        while not self._stop_evt.is_set():
            try:
                active = self.poll_once()
            except Exception as exc:
                logger.warning("Game detector poll failed: %s", exc)
                active = None

            if active != self._current:
                prev_name = self._current.display_name if self._current else "(none)"
                new_name = active.display_name if active else "(none)"
                logger.info("Active game: %s -> %s", prev_name, new_name)
                self._current = active
                try:
                    self._on_change(active)
                except Exception:
                    logger.exception("on_change callback raised")

            self._stop_evt.wait(self._poll_interval)


__all__ = ["GameDetector", "GameProfile"]
