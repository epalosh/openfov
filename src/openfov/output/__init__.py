"""Game output: FreeTrack shared memory writer, NPClient bootstrap,
TrackIR.exe shim, and the orchestrating OutputManager."""

from openfov.output.freetrack import FreeTrackWriter, FTHeap
from openfov.output.manager import GameOutputProfile, OutputManager
from openfov.output.npclient_bootstrap import (
    bundled_bin_dir,
    ensure_registered,
    normalize_dll_dir,
    read_registry_path,
    remove_registry_path,
    verify_registration,
)
from openfov.output.trackir_shim import TrackIRShim

__all__ = [
    "FTHeap",
    "FreeTrackWriter",
    "GameOutputProfile",
    "OutputManager",
    "TrackIRShim",
    "bundled_bin_dir",
    "ensure_registered",
    "normalize_dll_dir",
    "read_registry_path",
    "remove_registry_path",
    "verify_registration",
]
