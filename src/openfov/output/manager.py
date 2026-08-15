"""OutputManager — orchestrates the freestanding output stack.

Composes:
- FreeTrackWriter   (writes the shared memory)
- TrackIRShim       (launches dummy TrackIR.exe)
- NPClient registry (set on first start, never re-checked unless
                     `force_register` is True)

Plus per-game settings: GameID + 8-byte XOR encryption key. Updating the
active game is `set_game(profile)` — drops the writer in/out of the
right encryption mode without restarting.

Lifecycle is two-phase:
    mgr = OutputManager()
    mgr.start()                       # registers NPClient + launches dummy
    mgr.set_game(iracing_profile)     # configures encryption / GameID
    mgr.write(pose)                   # call every tracker frame
    mgr.stop()                        # tears down in reverse order

Idempotent — start() and stop() can be called repeatedly without harm."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from openfov.output.freetrack import FreeTrackWriter
from openfov.output.npclient_bootstrap import ensure_registered
from openfov.output.trackir_shim import TrackIRShim
from openfov.tracker.base import Pose6DOF

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GameOutputProfile:
    """Per-game wire-format tuning for the output stack.

    `game_id` is the value games pass to NP_RegisterProgramProfileID; our
    NPClient writes it back into shared memory's `GameID` field. The
    `encryption_key` is what NPClient XORs onto the returned pose for
    games that expect TrackIR-style encryption (most modern titles don't —
    leave it zero).
    """

    game_id: int = 0
    encryption_key: bytes = b"\x00" * 8


class OutputManager:
    """Owner of the full output stack."""

    def __init__(self) -> None:
        self._writer = FreeTrackWriter()
        self._shim = TrackIRShim()
        self._registered = False
        self._running = False
        self._current_profile: GameOutputProfile | None = None
        self._trackir_required = False

    @property
    def is_running(self) -> bool:
        return self._running

    # -- lifecycle ------------------------------------------------------

    def start(self, *, force_register: bool = False) -> None:
        """Bring the output stack up. Safe to call multiple times.

        Note the TrackIR.exe helper is NOT started here — see
        `set_trackir_required`. Launching it unconditionally meant every
        user ran a binary that most games don't need and that antivirus
        engines dislike."""
        if self._running:
            return
        if not self._registered or force_register:
            try:
                ensure_registered()
                self._registered = True
            except Exception as exc:
                logger.warning("NPClient registry update failed: %s", exc)
        self._writer.open()
        self._running = True
        logger.info("OutputManager started")

    def set_trackir_required(self, required: bool) -> None:
        """Start or stop the TrackIR.exe presence helper.

        Only a minority of titles (Falcon BMS, parts of MSFS) check the
        process list for a running TrackIR.exe; iRacing and most others
        find us purely through the NPClient registry key. Driven by
        `GameProfile.requires_trackir_process` so the helper stays off the
        default path — it is the component antivirus engines flag."""
        if required == self._trackir_required:
            return
        self._trackir_required = required
        if not self._running:
            return
        if required:
            logger.info("Active game needs a TrackIR.exe process; starting helper")
            self._shim.start()
        else:
            self._shim.stop()

    def stop(self) -> None:
        if not self._running:
            return
        self._shim.stop()          # no-op if it was never started
        self._trackir_required = False
        self._writer.close()
        self._running = False
        logger.info("OutputManager stopped")

    def __enter__(self) -> OutputManager:
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.stop()

    # -- per-game settings ---------------------------------------------

    def set_game(self, profile: GameOutputProfile) -> None:
        """Switch GameID + XOR encryption key. NPClient picks the change up
        on its next read (it inspects GameID == GameID2 to detect changes).

        Subtlety worth knowing: the only thing the GameID handshake buys us
        is handing NPClient a per-game XOR table. When the key is all zeros
        — true for iRacing and every profile we currently ship — there is
        no table to hand over, so we publish 0 instead of the game's ID.
        That leaves the game's own `NP_RegisterProgramProfileID` write as
        the sole source of a `GameId != GameId2` inequality, which is what
        `connected_game_id()` reads as proof a game is really talking to
        us. Publishing the game's true ID here would mask that signal.
        """
        if self._current_profile == profile:
            return
        needs_table = any(profile.encryption_key)
        self._writer.set_game_id(profile.game_id if needs_table else 0)
        self._writer.set_encryption_key(profile.encryption_key)
        self._current_profile = profile
        logger.info(
            "Output profile -> game_id=%d (published=%d, key=%s)",
            profile.game_id,
            profile.game_id if needs_table else 0,
            "set" if needs_table else "none",
        )

    def connected_game_id(self) -> int | None:
        """Program-profile ID of a game that has loaded our NPClient and
        registered itself, or None. See FreeTrackWriter.detected_client_game_id."""
        return self._writer.detected_client_game_id()

    def set_camera_dimensions(self, width: int, height: int) -> None:
        """Update the CamWidth/CamHeight fields — some games use these as
        diagnostic info or for native-resolution display alongside pose."""
        self._writer.set_camera_dimensions(width, height)

    # -- per-frame ------------------------------------------------------

    def write(self, pose: Pose6DOF) -> None:
        """Push one pose. If the manager isn't running yet, drops silently
        — caller doesn't need a `if running:` guard around it."""
        if not self._running:
            return
        self._writer.write(pose)


__all__ = ["GameOutputProfile", "OutputManager"]
