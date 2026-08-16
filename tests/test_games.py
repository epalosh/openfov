"""Game profile + registry + detector tests.

Detector poll-once goes through psutil — on a normal dev/CI machine
neither iRacingSim64DX11 nor any other registered game is running, so we
get None back. We deliberately avoid mocking the OS layer; we just verify
the polling path runs without error and returns the right thing for the
empty case."""

from __future__ import annotations

from openfov.games import (
    BUILTIN_PROFILES,
    IRACING_PROFILE,
    GameDetector,
    GameProfile,
    get_profile,
)


def test_iracing_profile_metadata() -> None:
    assert IRACING_PROFILE.id == "iracing"
    assert IRACING_PROFILE.display_name == "iRacing"
    assert "iRacingSim64DX11.exe" in IRACING_PROFILE.process_names
    # 14101 is what iRacing actually passes to NP_RegisterProgramProfileID
    # (observed in FT_SharedMem's GameId against the 2026.06 DX11 build).
    # Releases through 0.2.1 guessed 1001.
    assert IRACING_PROFILE.output.game_id == 14101
    assert IRACING_PROFILE.output.encryption_key == b"\x00" * 8


def test_iracing_in_builtin_registry() -> None:
    assert IRACING_PROFILE in BUILTIN_PROFILES


def test_get_profile_by_id() -> None:
    assert get_profile("iracing") is IRACING_PROFILE
    assert get_profile("nonexistent") is None


def test_detector_poll_returns_none_when_no_game_running() -> None:
    """The poll path must not crash and must return None when no
    matching process is alive.

    We register a profile keyed on a fictional executable name so the
    test passes regardless of whether the developer happens to have
    iRacing open right now."""
    fake = GameProfile(
        id="fake",
        display_name="Fake Game",
        process_names=("DefinitelyNotARealProcess_zk72.exe",),
    )
    captured: list[GameProfile | None] = []
    det = GameDetector([fake], on_change=captured.append)
    # poll_once is synchronous, doesn't need start().
    result = det.poll_once()
    assert result is None
    # No on_change callbacks fired (we never called start()).
    assert captured == []


def test_detector_proc_to_profile_lowercased() -> None:
    """The process-name lookup table is case-insensitive."""
    profile = GameProfile(
        id="test",
        display_name="Test",
        process_names=("MyGame.EXE",),
    )
    det = GameDetector([profile], on_change=lambda _p: None)
    # Internal lookup is lowercased.
    assert "mygame.exe" in det._proc_to_profile


def test_detector_start_stop_idempotent() -> None:
    det = GameDetector([IRACING_PROFILE], on_change=lambda _p: None, poll_interval_s=0.05)
    det.start()
    det.start()  # no-op
    det.stop()
    det.stop()  # no-op


def test_detector_no_profiles_is_legal() -> None:
    """Edge case: empty profile list. Detector must still construct."""
    det = GameDetector([], on_change=lambda _p: None)
    assert det.poll_once() is None
