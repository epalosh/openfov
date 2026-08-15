"""Camera source.

Wraps `cv2.VideoCapture` with:
- Friendly-name enumeration via `cv2-enumerate-cameras` (so the UI can show
  "Logitech BRIO" instead of "0").
- Media Foundation preferred over DirectShow on modern Windows; DSHOW
  fallback if MSMF fails (some older USB-UVC drivers).
- Mirrored output (we want users to see themselves naturally).
- Resolution + FPS setters that gracefully accept the closest available
  mode the device supports.

Cross-platform: on non-Windows we still use OpenCV's default backend.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterable
from dataclasses import dataclass

import cv2
import numpy as np

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CameraInfo:
    """A discoverable camera."""

    index: int
    name: str
    backend: str = ""

    @property
    def display_label(self) -> str:
        # Deliberately does NOT lead with `index`. On Windows the index is
        # backend-encoded (1400 = MSMF + device 0, 700 = DSHOW + device 0),
        # so the old "1400: USB Video Device" label showed users a magic
        # number and, worse, listed the same physical webcam twice.
        if not self.name:
            return f"Camera {self.index}"
        return f"{self.name} ({self.backend})" if self.backend else self.name


# Windows capture backends, best first. MSMF is the modern path and what
# CameraSource prefers; DSHOW is the legacy fallback for older UVC drivers.
_BACKEND_NAMES = {
    cv2.CAP_MSMF: "Media Foundation",
    cv2.CAP_DSHOW: "DirectShow",
}
_BACKEND_ORDER = (cv2.CAP_MSMF, cv2.CAP_DSHOW)


def _backend_of(index: int) -> int:
    """cv2-enumerate-cameras encodes the backend into the index it hands
    back (`CAP_* + ordinal`). Recover it so we can rank duplicates."""
    for base in _BACKEND_ORDER:
        if base <= index < base + 100:
            return base
    return cv2.CAP_ANY


def _device_key(cam) -> object:
    """Identity of the *physical* device, independent of backend.

    The USB device path is identical across backends apart from the
    trailing interface GUID, so trimming that gives a stable key. Falls
    back to vendor/product/name when no path is exposed."""
    path = getattr(cam, "path", None)
    if path:
        return path.split("#{")[0].lower()
    return (getattr(cam, "vid", None), getattr(cam, "pid", None), cam.name)


def enumerate_cameras() -> list[CameraInfo]:
    """List available cameras with friendly names, one entry per physical
    device.

    Returns at least an empty list (never raises). On Windows it tries
    cv2-enumerate-cameras for friendly names; otherwise it probes indices
    0..N as a fallback.

    Windows exposes each webcam once per capture backend, so a single
    camera used to appear twice ("1400: USB Video Device" and "700: USB
    Video Device") with no way for the user to tell which to pick. We
    collapse those to one entry, preferring Media Foundation."""
    cams: list[CameraInfo] = []
    if sys.platform == "win32":
        try:
            from cv2_enumerate_cameras import enumerate_cameras as _enum

            best: dict[object, tuple[int, CameraInfo]] = {}
            for c in _enum():
                backend = _backend_of(c.index)
                rank = (
                    _BACKEND_ORDER.index(backend)
                    if backend in _BACKEND_ORDER
                    else len(_BACKEND_ORDER)
                )
                info = CameraInfo(
                    index=c.index,
                    name=c.name or "Unknown",
                    backend=_BACKEND_NAMES.get(backend, ""),
                )
                key = _device_key(c)
                existing = best.get(key)
                if existing is None or rank < existing[0]:
                    best[key] = (rank, info)
            cams = [info for _, info in best.values()]
        except Exception as exc:
            logger.debug("cv2-enumerate-cameras unavailable, falling back to probe: %s", exc)

    if not cams:
        # Generic probe — try indices 0..4 with the default backend.
        for idx in range(5):
            cap = cv2.VideoCapture(idx)
            opened = cap.isOpened()
            cap.release()
            if opened:
                cams.append(CameraInfo(index=idx, name=f"Camera {idx}"))

    # Disambiguate identical names across genuinely different devices
    # (two of the same webcam model) so the list is still actionable.
    seen: dict[str, int] = {}
    for c in cams:
        seen[c.name] = seen.get(c.name, 0) + 1
    if any(n > 1 for n in seen.values()):
        numbered: list[CameraInfo] = []
        counters: dict[str, int] = {}
        for c in cams:
            if seen[c.name] > 1:
                counters[c.name] = counters.get(c.name, 0) + 1
                numbered.append(
                    CameraInfo(
                        index=c.index,
                        name=f"{c.name} #{counters[c.name]}",
                        backend=c.backend,
                    )
                )
            else:
                numbered.append(c)
        cams = numbered

    return cams


class CameraSource:
    """Wraps a single VideoCapture with backend selection + safe lifecycle.

    Usage:
        cam = CameraSource(index=0, width=1280, height=720)
        cam.open()
        ok, frame_bgr = cam.read()
        cam.close()

    `read()` returns `(False, None)` when the device drops (rather than
    raising) so the pipeline thread can decide whether to retry."""

    def __init__(
        self,
        index: int = 0,
        width: int = 1280,
        height: int = 720,
        fps: float = 60.0,
        mirror: bool = True,
    ) -> None:
        self.index = index
        self.width = width
        self.height = height
        # We *target* 60 fps and let the device tell us what it can
        # actually deliver via CAP_PROP_FPS read-back. Cameras that
        # can't hit 60 silently negotiate down to 30 (or whatever
        # their best supported mode is) — no error, just a slower
        # capture rate.
        self.fps = fps
        self.mirror = mirror
        self._cap: cv2.VideoCapture | None = None
        self._backend_name: str = ""
        self._fourcc_negotiated: str = ""

    @property
    def is_open(self) -> bool:
        return self._cap is not None and self._cap.isOpened()

    @property
    def backend_name(self) -> str:
        return self._backend_name

    @property
    def actual_size(self) -> tuple[int, int]:
        """The width/height the device negotiated, which may differ from
        what we requested if the camera doesn't support the exact mode."""
        if self._cap is None:
            return (self.width, self.height)
        w = int(self._cap.get(cv2.CAP_PROP_FRAME_WIDTH) or self.width)
        h = int(self._cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or self.height)
        return (w, h)

    @property
    def actual_fps(self) -> float:
        """The FPS the device negotiated. May differ from what we requested.
        Some webcams report nonsense (0, or the next-lower standard frame
        rate); the value is best-effort, not authoritative."""
        if self._cap is None:
            return self.fps
        v = float(self._cap.get(cv2.CAP_PROP_FPS) or 0.0)
        return v if v > 0 else self.fps

    @property
    def fourcc_name(self) -> str:
        """Negotiated pixel format ('MJPG', 'YUY2', etc.). Empty when closed."""
        return self._fourcc_negotiated

    def open(self) -> bool:
        """Open the camera. Returns True on success.

        Backend order on Windows: ANY (honors encoded indices from
        cv2-enumerate-cameras, which pack the backend into the high
        digits, e.g. 1400 = MSMF + dev 0), then explicit MSMF/DSHOW for
        plain integer indices. On other platforms: ANY only."""
        if self.is_open:
            return True

        backends: Iterable[tuple[int, str]]
        if sys.platform == "win32":
            # ANY first — decodes the backend from encoded indices like
            # 1400/700/701 that cv2-enumerate-cameras hands us. Explicit
            # backends second, for the rare case of a literal "0" / "1".
            backends = (
                (cv2.CAP_ANY, "ANY"),
                (cv2.CAP_MSMF, "MSMF"),
                (cv2.CAP_DSHOW, "DSHOW"),
            )
        else:
            backends = ((cv2.CAP_ANY, "ANY"),)

        for backend, name in backends:
            try:
                cap = cv2.VideoCapture(self.index, backend)
            except cv2.error as exc:
                logger.debug("VideoCapture(%d, %s) raised: %s", self.index, name, exc)
                continue
            if not cap.isOpened():
                logger.debug("Camera index=%d on %s: not opened", self.index, name)
                cap.release()
                continue
            # OpenCV's `.set()` can raise an "Unknown C++ exception" when a
            # backend is in a bad state (common right after a rapid
            # disconnect/reconnect, or when switching backends). Treat
            # any property-set failure as "this backend doesn't want to
            # be configured" — move on to the next, don't crash.
            try:
                # MJPG must be requested *before* resolution + FPS to
                # actually unlock high frame rates. Most consumer webcams
                # report two pixel formats over USB:
                #   - YUYV (uncompressed) — typically caps at 30 fps for
                #     720p/1080p due to USB 2.0 bandwidth limits
                #   - MJPG (motion JPEG) — supports 60+ fps at the same
                #     resolution because each frame is compressed
                # If we don't set FOURCC explicitly the camera negotiates
                # YUYV by default and our `CAP_PROP_FPS = 60` request is
                # silently ignored. Setting MJPG first lets the camera
                # pick the high-rate mode. Cameras that don't support
                # MJPG (some integrated laptop cams) silently keep their
                # default mode and we still get a working capture —
                # just at 30 fps. No error path needed.
                mjpg = cv2.VideoWriter_fourcc(*"MJPG")
                cap.set(cv2.CAP_PROP_FOURCC, mjpg)
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
                cap.set(cv2.CAP_PROP_FPS, self.fps)
                # MSMF buffers up to ~5 frames by default which adds latency.
                # Force-set buffer-size = 1 if the backend honors it.
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            except cv2.error as exc:
                logger.warning(
                    "Camera %d on %s failed during property set: %s — trying next backend",
                    self.index, name, exc,
                )
                cap.release()
                continue
            self._cap = cap
            self._backend_name = name
            # Read back the negotiated FOURCC for diagnostic logging.
            # Cameras that genuinely support 60 fps will report MJPG;
            # cameras that fell back to default pixel format report
            # YUYV/YUY2 or similar — usually pairs with a 30 fps cap.
            try:
                raw = int(cap.get(cv2.CAP_PROP_FOURCC) or 0)
                self._fourcc_negotiated = (
                    "".join(chr((raw >> (8 * i)) & 0xFF) for i in range(4)).strip()
                    if raw
                    else ""
                )
            except (cv2.error, ValueError):
                self._fourcc_negotiated = ""
            logger.info(
                "Camera index=%d opened via %s at %dx%d @ %.0f fps (%s)",
                self.index, name, *self.actual_size,
                self.actual_fps, self._fourcc_negotiated or "?",
            )
            return True

        logger.error(
            "Could not open camera index=%d on any backend. If another app "
            "(Zoom, Teams, OBS) is holding the camera, close it and try again.",
            self.index,
        )
        return False

    def read(self) -> tuple[bool, np.ndarray | None]:
        """Read one frame. Applies mirror flip if enabled. Returns (False,
        None) on failure — pipeline decides whether to retry or escalate.
        Any OpenCV exception is treated as a soft failure (we count it
        toward the hot-plug disconnect threshold)."""
        if self._cap is None:
            return False, None
        try:
            ok, frame = self._cap.read()
        except cv2.error as exc:
            logger.debug("VideoCapture.read raised: %s", exc)
            return False, None
        if not ok or frame is None:
            return False, None
        try:
            if self.mirror:
                frame = cv2.flip(frame, 1)
        except cv2.error as exc:
            logger.debug("cv2.flip raised: %s", exc)
            return False, None
        return True, frame

    def close(self) -> None:
        if self._cap is not None:
            self._cap.release()
            self._cap = None
            self._backend_name = ""
            self._fourcc_negotiated = ""

    def __enter__(self) -> CameraSource:
        self.open()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


__all__ = ["CameraInfo", "CameraSource", "enumerate_cameras"]
