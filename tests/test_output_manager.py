"""OutputManager lifecycle + GameOutputProfile switching tests.

Note that `OutputManager.start()` *does* hit the Windows-only paths — it
calls `ensure_registered()` and `TrackIRShim.start()`. The autouse fixture
in conftest.py redirects the registry write and empties the bin dir so
neither touches real machine state; without it, running this file on
Windows repoints the user's actual NaturalPoint key at the checkout.

What we assert cross-platform:
- start()/stop() are idempotent
- set_game() updates state, but doesn't break on no-op
- write() is silent when not running
"""

from __future__ import annotations

import pytest

from openfov.output.manager import GameOutputProfile, OutputManager
from openfov.tracker.base import Pose6DOF


def test_start_stop_idempotent() -> None:
    mgr = OutputManager()
    mgr.start()
    mgr.start()  # no-op
    assert mgr.is_running
    mgr.stop()
    mgr.stop()  # no-op
    assert not mgr.is_running


def test_context_manager() -> None:
    with OutputManager() as mgr:
        assert mgr.is_running
    assert not mgr.is_running


def test_write_without_start_is_silent() -> None:
    mgr = OutputManager()
    mgr.write(Pose6DOF(yaw=10.0))  # no exception


def test_set_game_updates_profile() -> None:
    profile_a = GameOutputProfile(game_id=1001)
    profile_b = GameOutputProfile(game_id=2002, encryption_key=b"\x01\x02\x03\x04\x05\x06\x07\x08")
    with OutputManager() as mgr:
        mgr.set_game(profile_a)
        assert mgr._current_profile == profile_a
        mgr.set_game(profile_b)
        assert mgr._current_profile == profile_b


def test_set_game_same_profile_skips() -> None:
    """Setting the same profile twice should not re-write the writer fields."""
    profile = GameOutputProfile(game_id=1001)
    with OutputManager() as mgr:
        mgr.set_game(profile)
        mgr.set_game(profile)  # no-op path
        assert mgr._current_profile == profile


def test_invalid_encryption_key_rejected() -> None:
    """Encryption keys must be exactly 8 bytes."""
    profile = GameOutputProfile(game_id=1001, encryption_key=b"\x01\x02\x03")
    with OutputManager() as mgr, pytest.raises(ValueError):
        mgr.set_game(profile)


# ---------------------------------------------------------------------------
# TrackIR.exe helper gating
# ---------------------------------------------------------------------------


def test_shim_not_started_by_start() -> None:
    """start() must NOT launch the TrackIR.exe helper. Most games don't
    need it and it's the component antivirus engines flag."""
    mgr = OutputManager()
    mgr.start()
    try:
        assert not mgr._shim.is_running
    finally:
        mgr.stop()


def test_set_trackir_required_is_idempotent() -> None:
    mgr = OutputManager()
    mgr.start()
    try:
        mgr.set_trackir_required(False)
        mgr.set_trackir_required(False)
        assert not mgr._shim.is_running
    finally:
        mgr.stop()


# ---------------------------------------------------------------------------
# Connected-game detection
# ---------------------------------------------------------------------------


def test_zero_key_profile_publishes_game_id_zero() -> None:
    """With no encryption table to hand over there is nothing to gain from
    publishing the game's ID, and publishing it would mask the
    GameId != GameId2 signal we use to detect a connected game."""
    profile = GameOutputProfile(game_id=14101, encryption_key=b"\x00" * 8)
    with OutputManager() as mgr:
        mgr.set_game(profile)
        assert mgr._writer._game_id == 0


def test_nonzero_key_profile_publishes_real_game_id() -> None:
    """When there *is* a table, NPClient only picks it up while
    GameId == GameId2, so we must publish the real ID."""
    profile = GameOutputProfile(game_id=2002, encryption_key=b"\x01" * 8)
    with OutputManager() as mgr:
        mgr.set_game(profile)
        assert mgr._writer._game_id == 2002


def test_no_client_detected_when_nothing_connected() -> None:
    with OutputManager() as mgr:
        mgr.set_game(GameOutputProfile(game_id=14101))
        assert mgr.connected_game_id() is None
