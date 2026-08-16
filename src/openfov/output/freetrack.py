"""FreeTrack shared-memory writer.

Implements the wire format that opentrack's `proto-ft` writes and that the
bundled NPClient.dll reads. Layout, names, and conventions follow
`opentrack/freetrackclient/fttypes.h` and `opentrack/proto-ft/ftnoir_protocol_ft.cpp`:

- Shared section name: ``FT_SharedMem``
- Mutex name: ``FT_Mutext`` (intentional typo — must match exactly)
- Total `FTHeap` size: ~228 bytes
- Angles stored as `float` in radians
- Translation stored as `float` in millimeters
- Sign convention: +yaw = look left, +pitch = up, +roll = left ear down
- `DataID` is incremented atomically each write

We write *only* to `FT_SharedMem`. NPClient.dll handles per-game XOR
encryption on the read side; we put the encryption key into the
`EncryptionKey` field of the heap (zeros = no encryption, NPClient's
default for unknown games).

Platform: Windows only. ctypes against kernel32.

References:
- https://github.com/opentrack/opentrack/blob/master/freetrackclient/fttypes.h
- https://github.com/opentrack/opentrack/blob/master/proto-ft/ftnoir_protocol_ft.cpp
- https://github.com/opentrack/opentrack/blob/master/contrib/npclient/npclient.c
"""

from __future__ import annotations

import ctypes
import math
import sys
from ctypes import wintypes

from openfov.tracker.base import Pose6DOF

# ---------------------------------------------------------------------------
# FTHeap layout — keep in sync with opentrack/freetrackclient/fttypes.h
# ---------------------------------------------------------------------------


class FTData(ctypes.Structure):
    """Mirror of opentrack's `FTData` struct.

    Field order and types matter — this is the wire format. Do not reorder.
    """

    _pack_ = 1
    _fields_ = [
        ("DataID", ctypes.c_uint32),
        ("CamWidth", ctypes.c_int32),
        ("CamHeight", ctypes.c_int32),
        # Smoothed pose (what games actually read).
        ("Yaw", ctypes.c_float),
        ("Pitch", ctypes.c_float),
        ("Roll", ctypes.c_float),
        ("X", ctypes.c_float),
        ("Y", ctypes.c_float),
        ("Z", ctypes.c_float),
        # Raw pose (some games can opt to read this). We mirror the smoothed
        # values since OpenFOV's own smoothing happens upstream.
        ("RawYaw", ctypes.c_float),
        ("RawPitch", ctypes.c_float),
        ("RawRoll", ctypes.c_float),
        ("RawX", ctypes.c_float),
        ("RawY", ctypes.c_float),
        ("RawZ", ctypes.c_float),
        # 4 reference points (sorted by Y, origin top-left) — legacy
        # FreeTrack IR-LED tracking carry-over. We zero them.
        ("X1", ctypes.c_float), ("Y1", ctypes.c_float),
        ("X2", ctypes.c_float), ("Y2", ctypes.c_float),
        ("X3", ctypes.c_float), ("Y3", ctypes.c_float),
        ("X4", ctypes.c_float), ("Y4", ctypes.c_float),
    ]


class FTHeap(ctypes.Structure):
    """Mirror of opentrack's `FTHeap` struct."""

    _pack_ = 1
    _fields_ = [
        ("data", FTData),
        ("GameID", ctypes.c_int32),
        # Anonymous union in C; we expose just the byte view since that's
        # what NPClient.dll reads. The ints view (`table_ints[2]`) overlaps
        # the same 8 bytes.
        ("EncryptionKey", ctypes.c_ubyte * 8),
        ("GameID2", ctypes.c_int32),
    ]


SHARED_MEM_NAME = "FT_SharedMem"
MUTEX_NAME = "FT_Mutext"  # intentional typo — must match opentrack + NPClient


# ---------------------------------------------------------------------------
# Win32 plumbing
# ---------------------------------------------------------------------------

if sys.platform == "win32":
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _CreateFileMappingW = _kernel32.CreateFileMappingW
    _CreateFileMappingW.argtypes = [
        wintypes.HANDLE,         # hFile
        ctypes.c_void_p,         # lpFileMappingAttributes (SECURITY_ATTRIBUTES*)
        wintypes.DWORD,          # flProtect
        wintypes.DWORD,          # dwMaximumSizeHigh
        wintypes.DWORD,          # dwMaximumSizeLow
        wintypes.LPCWSTR,        # lpName
    ]
    _CreateFileMappingW.restype = wintypes.HANDLE

    _MapViewOfFile = _kernel32.MapViewOfFile
    _MapViewOfFile.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.c_size_t,
    ]
    _MapViewOfFile.restype = ctypes.c_void_p

    _UnmapViewOfFile = _kernel32.UnmapViewOfFile
    _UnmapViewOfFile.argtypes = [ctypes.c_void_p]
    _UnmapViewOfFile.restype = wintypes.BOOL

    _CreateMutexW = _kernel32.CreateMutexW
    _CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    _CreateMutexW.restype = wintypes.HANDLE

    _WaitForSingleObject = _kernel32.WaitForSingleObject
    _WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    _WaitForSingleObject.restype = wintypes.DWORD

    _ReleaseMutex = _kernel32.ReleaseMutex
    _ReleaseMutex.argtypes = [wintypes.HANDLE]
    _ReleaseMutex.restype = wintypes.BOOL

    _CloseHandle = _kernel32.CloseHandle
    _CloseHandle.argtypes = [wintypes.HANDLE]
    _CloseHandle.restype = wintypes.BOOL

    _INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value
    _PAGE_READWRITE = 0x04
    _FILE_MAP_WRITE = 0x0002
    # Mutex acquisition timeout. opentrack uses 16 ms (one frame at 60
    # fps) but that's a latency bomb for us: a single contended write
    # stalls inference for a full frame interval. We use 1 ms — if we
    # can't get the mutex that fast it means iRacing is mid-read, and
    # we'd rather drop *this* write (game keeps last-good pose for one
    # extra frame) than freeze inference. At 60+ fps a missed write is
    # invisible; a 16 ms inference stall is not.
    _MUTEX_TIMEOUT_MS = 1
    _WAIT_OBJECT_0 = 0x0
    _WAIT_TIMEOUT = 0x102


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------


class FreeTrackWriter:
    """Writes 6DOF pose to the FT_SharedMem named mapping.

    Lifecycle:
        w = FreeTrackWriter()
        w.open()
        w.write(pose)                   # call as often as you have new data
        w.close()                       # idempotent

    Thread-safety: writes use the FT_Mutext mutex with a 16 ms timeout. Safe
    to call from one thread; the mutex protects against the NPClient reader
    on the game side, not against multi-writer scenarios on our side.

    On non-Windows platforms (CI on Linux, dev on macOS), `open()` becomes a
    no-op and `write()` is a no-op — this lets the headless test pipeline
    run on any OS while keeping the production path Windows-only."""

    def __init__(self, frame_width: int = 0, frame_height: int = 0, game_id: int = 0) -> None:
        self._frame_w = frame_width
        self._frame_h = frame_height
        self._game_id = game_id
        self._mapping_handle: int | None = None
        self._mutex_handle: int | None = None
        self._view: ctypes.c_void_p | None = None
        self._heap: FTHeap | None = None
        # Telemetry: writes attempted vs writes that actually committed
        # (i.e. acquired the mutex). The pipeline samples these to
        # surface "are my poses actually reaching iRacing?" stats.
        self.writes_committed = 0
        self.writes_dropped = 0

    # -- introspection --------------------------------------------------

    @staticmethod
    def expected_heap_size() -> int:
        """Total size of the FTHeap struct. Used as a sanity check in tests
        and as the mapping size when opening the shared section."""
        return ctypes.sizeof(FTHeap)

    @property
    def is_open(self) -> bool:
        return self._view is not None

    # -- lifecycle ------------------------------------------------------

    def open(self) -> None:
        """Create (or open) the FT_SharedMem mapping and FT_Mutext mutex.

        No-op on non-Windows."""
        if sys.platform != "win32":
            return
        if self._view is not None:
            return

        size = self.expected_heap_size()
        # CreateFileMapping with INVALID_HANDLE_VALUE = anonymous (backed by
        # paging file). If a mapping with this name already exists, the call
        # returns a handle to the existing one — same behavior as opentrack.
        handle = _CreateFileMappingW(
            _INVALID_HANDLE_VALUE,
            None,
            _PAGE_READWRITE,
            0,
            size,
            SHARED_MEM_NAME,
        )
        if not handle:
            err = ctypes.get_last_error()
            raise OSError(err, f"CreateFileMappingW({SHARED_MEM_NAME!r}) failed")
        self._mapping_handle = handle

        view = _MapViewOfFile(handle, _FILE_MAP_WRITE, 0, 0, size)
        if not view:
            err = ctypes.get_last_error()
            _CloseHandle(handle)
            self._mapping_handle = None
            raise OSError(err, "MapViewOfFile failed")
        self._view = ctypes.c_void_p(view)
        # Cast the raw memory to our FTHeap struct.
        self._heap = ctypes.cast(view, ctypes.POINTER(FTHeap)).contents

        # Zero-initialize. Some readers can see stale memory from a prior
        # writer otherwise.
        ctypes.memset(view, 0, size)
        self._heap.data.CamWidth = self._frame_w
        self._heap.data.CamHeight = self._frame_h
        self._heap.GameID = self._game_id
        self._heap.GameID2 = self._game_id

        mutex = _CreateMutexW(None, False, MUTEX_NAME)
        if not mutex:
            err = ctypes.get_last_error()
            _UnmapViewOfFile(view)
            _CloseHandle(handle)
            self._mapping_handle = None
            self._view = None
            self._heap = None
            raise OSError(err, f"CreateMutexW({MUTEX_NAME!r}) failed")
        self._mutex_handle = mutex

    def close(self) -> None:
        if sys.platform != "win32":
            return
        if self._mutex_handle is not None:
            _CloseHandle(self._mutex_handle)
            self._mutex_handle = None
        if self._view is not None:
            _UnmapViewOfFile(self._view)
            self._view = None
            self._heap = None
        if self._mapping_handle is not None:
            _CloseHandle(self._mapping_handle)
            self._mapping_handle = None

    def __enter__(self) -> FreeTrackWriter:
        self.open()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # -- writes ---------------------------------------------------------

    def write(self, pose: Pose6DOF) -> None:
        """Push one pose into the shared memory.

        Sign-conventions match opentrack's writer: yaw and pitch are
        negated before storage, roll is passed through. Angles are
        converted from degrees to radians; translation is left in mm.

        Mutex semantics: we ONLY write when we actually hold the mutex.
        If acquisition times out (iRacing is mid-read), we drop the
        write — the game keeps its last-good pose for one extra frame.
        Previous code wrote even on timeout, which produced torn reads:
        DataID incremented while pose fields were half-old/half-new,
        and iRacing reportedly ignores those updates entirely.

        No-op on non-Windows (lets headless CI / Linux dev run without
        special-casing)."""
        if sys.platform != "win32" or self._heap is None or self._mutex_handle is None:
            return

        rc = _WaitForSingleObject(self._mutex_handle, _MUTEX_TIMEOUT_MS)
        if rc != _WAIT_OBJECT_0:
            # Either timed out (iRacing reading) or hard failure — drop
            # the write cleanly. Atomic int increment is GIL-protected
            # in CPython so the counter is safe without locking.
            self.writes_dropped += 1
            return

        try:
            d = self._heap.data
            d.DataID = (d.DataID + 1) & 0xFFFFFFFF

            yaw_rad = math.radians(-pose.yaw)
            pitch_rad = math.radians(-pose.pitch)
            roll_rad = math.radians(pose.roll)

            d.Yaw = yaw_rad
            d.Pitch = pitch_rad
            d.Roll = roll_rad
            d.X = pose.x
            d.Y = pose.y
            d.Z = pose.z

            d.RawYaw = yaw_rad
            d.RawPitch = pitch_rad
            d.RawRoll = roll_rad
            d.RawX = pose.x
            d.RawY = pose.y
            d.RawZ = pose.z
            self.writes_committed += 1
        finally:
            _ReleaseMutex(self._mutex_handle)

    def set_game_id(self, game_id: int) -> None:
        """Update the GameID fields. Triggers NPClient to reload the
        per-game encryption key on the read side.

        Both fields are written: NPClient only trusts the encryption table
        when `GameId == GameId2`, which is the protocol's torn-write guard.
        """
        self._game_id = game_id
        if self._heap is not None:
            self._heap.GameID = game_id
            self._heap.GameID2 = game_id

    def detected_client_game_id(self) -> int | None:
        """Return the program-profile ID of a game that has connected, or
        None if nothing has.

        How this works: we always write `GameId` and `GameId2` to the *same*
        value. NPClient's `NP_RegisterProgramProfileID`, running inside the
        game process, assigns `pMemData->GameId = id` and never touches
        `GameId2`. So an inequality between the two fields can only have
        been produced by a game that loaded our DLL and registered itself.

        That makes this a true end-to-end liveness check — it proves the
        game found the registry key, loaded NPClient64.dll, passed the
        signature check, and called into us. Nothing else in the app can
        observe that, which is why a broken registry layout went unnoticed
        through three releases.
        """
        if sys.platform != "win32" or self._heap is None:
            return None
        game_id = self._heap.GameID
        if game_id != self._heap.GameID2:
            return int(game_id)
        return None

    def set_encryption_key(self, key: bytes) -> None:
        """Set the 8-byte XOR key NPClient applies before returning data
        to the game. Pass 8 zero bytes to disable encryption (default for
        unknown games)."""
        if len(key) != 8:
            raise ValueError("encryption key must be exactly 8 bytes")
        if self._heap is not None:
            for i, b in enumerate(key):
                self._heap.EncryptionKey[i] = b

    def set_camera_dimensions(self, width: int, height: int) -> None:
        self._frame_w = width
        self._frame_h = height
        if self._heap is not None:
            self._heap.data.CamWidth = width
            self._heap.data.CamHeight = height
