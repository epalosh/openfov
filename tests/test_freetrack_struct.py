"""FreeTrack wire-format struct tests.

These tests don't write to shared memory (they pass on every OS). They
verify the ctypes layout matches the opentrack reference exactly — if the
struct changes size or field offsets, NPClient.dll will read garbage."""

from __future__ import annotations

import ctypes
import sys

import pytest

from openfov.output.freetrack import FreeTrackWriter, FTData, FTHeap


def test_ftdata_size_matches_reference() -> None:
    """FTData = 3 uint32 (DataID + CamW + CamH) + 6 float (smoothed pose)
    + 6 float (raw pose) + 8 float (4 IR points) = 3*4 + 20*4 = 92 bytes."""
    assert ctypes.sizeof(FTData) == 92


def test_ftheap_size_matches_reference() -> None:
    """FTHeap = FTData (92) + GameID (4) + key (8) + GameID2 (4) = 108."""
    assert ctypes.sizeof(FTHeap) == 108


def test_field_offsets_yaw_pitch_roll() -> None:
    """Yaw/Pitch/Roll come after DataID (4) + CamWidth (4) + CamHeight (4) = 12."""
    assert FTData.Yaw.offset == 12
    assert FTData.Pitch.offset == 16
    assert FTData.Roll.offset == 20


def test_field_offsets_x_y_z() -> None:
    assert FTData.X.offset == 24
    assert FTData.Y.offset == 28
    assert FTData.Z.offset == 32


def test_field_offsets_raw_block() -> None:
    """Raw block immediately follows smoothed (which ended at offset 36)."""
    assert FTData.RawYaw.offset == 36
    assert FTData.RawZ.offset == 56


def test_field_offsets_ir_points() -> None:
    """IR points start after raw block (offset 60)."""
    assert FTData.X1.offset == 60
    assert FTData.Y4.offset == 88


def test_ftheap_fields_in_correct_order() -> None:
    """GameID must come before EncryptionKey which must come before GameID2.

    NPClient reads them in this order to decide whether to apply XOR."""
    assert FTHeap.data.offset == 0
    assert FTHeap.GameID.offset == 92
    assert FTHeap.EncryptionKey.offset == 96
    assert FTHeap.GameID2.offset == 104


def test_writer_reports_expected_size() -> None:
    from openfov.output.freetrack import FreeTrackWriter

    assert FreeTrackWriter.expected_heap_size() == ctypes.sizeof(FTHeap)


# ---------------------------------------------------------------------------
# Connected-game detection
#
# npclient.c's NP_RegisterProgramProfileID does `pMemData->GameId = id` and
# never touches GameId2. We always write the two fields together, so an
# inequality can only have been produced by a game running our DLL. That is
# the only end-to-end proof the app has that the whole chain works, so pin
# the behaviour.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform != "win32", reason="shared memory is Windows-only")
def test_no_client_detected_when_fields_agree() -> None:
    w = FreeTrackWriter()
    w.open()
    try:
        w.set_game_id(0)
        assert w.detected_client_game_id() is None
        w.set_game_id(14101)
        assert w.detected_client_game_id() is None
    finally:
        w.close()


@pytest.mark.skipif(sys.platform != "win32", reason="shared memory is Windows-only")
def test_client_detected_when_game_overwrites_game_id() -> None:
    """Simulate exactly what NP_RegisterProgramProfileID does inside the
    game process: assign GameId, leave GameId2 alone."""
    w = FreeTrackWriter()
    w.open()
    try:
        w.set_game_id(0)
        assert w.detected_client_game_id() is None
        w._heap.GameID = 14101          # the game registers itself
        assert w.detected_client_game_id() == 14101
        w.set_game_id(0)                # we re-publish; signal clears
        assert w.detected_client_game_id() is None
    finally:
        w.close()


def test_detection_is_noop_when_closed() -> None:
    assert FreeTrackWriter().detected_client_game_id() is None
