"""Camera enumeration: one entry per physical device.

Windows exposes each webcam once per capture backend, so the picker used
to list a single camera twice — as "1400: USB Video Device" and "700: USB
Video Device" — with nothing to tell the user which to choose. Those
numbers are `cv2.CAP_MSMF + ordinal` and `cv2.CAP_DSHOW + ordinal`, i.e.
an implementation detail leaking into the UI.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

import cv2
import pytest

from openfov.runtime import camera as camera_mod
from openfov.runtime.camera import CameraInfo, _backend_of, _device_key, enumerate_cameras


@dataclass
class _FakeCam:
    index: int
    name: str
    vid: int = 0
    pid: int = 0
    path: str | None = None


# Real values captured from a Razer webcam on the test machine: same
# device, same USB instance, differing only in interface GUID.
_MSMF_PATH = (
    r"\\?\usb#vid_1532&pid_0e03&mi_00#6&15710041&0&0000"
    r"#{e5323777-f976-4f5b-9b55-b94699c46e44}\global"
)
_DSHOW_PATH = (
    r"\\?\usb#vid_1532&pid_0e03&mi_00#6&15710041&0&0000"
    r"#{65e8773d-8f56-11d0-a3b9-00a0c9223196}\global"
)


def test_backend_recovered_from_encoded_index() -> None:
    assert _backend_of(1400) == cv2.CAP_MSMF
    assert _backend_of(1401) == cv2.CAP_MSMF
    assert _backend_of(700) == cv2.CAP_DSHOW
    assert _backend_of(0) == cv2.CAP_ANY


def test_same_device_across_backends_has_one_key() -> None:
    a = _FakeCam(index=1400, name="USB Video Device", path=_MSMF_PATH)
    b = _FakeCam(index=700, name="USB Video Device", path=_DSHOW_PATH)
    assert _device_key(a) == _device_key(b)


def test_distinct_devices_have_distinct_keys() -> None:
    a = _FakeCam(index=1400, name="Cam", path=_MSMF_PATH)
    b = _FakeCam(
        index=1401,
        name="Cam",
        path=r"\\?\usb#vid_1532&pid_0e03&mi_00#6&99999999&0&0000#{e5323777}\global",
    )
    assert _device_key(a) != _device_key(b)


def test_device_key_falls_back_to_vid_pid_name() -> None:
    a = _FakeCam(index=1400, name="Cam", vid=1, pid=2, path=None)
    b = _FakeCam(index=700, name="Cam", vid=1, pid=2, path=None)
    assert _device_key(a) == _device_key(b)


@pytest.mark.skipif(sys.platform != "win32", reason="dedupe path is Windows-only")
def test_duplicate_backends_collapse_to_one_entry(monkeypatch) -> None:
    fake = [
        _FakeCam(index=1400, name="USB Video Device", path=_MSMF_PATH),
        _FakeCam(index=700, name="USB Video Device", path=_DSHOW_PATH),
    ]
    monkeypatch.setitem(
        sys.modules,
        "cv2_enumerate_cameras",
        type(sys)("cv2_enumerate_cameras"),
    )
    sys.modules["cv2_enumerate_cameras"].enumerate_cameras = lambda: fake

    cams = enumerate_cameras()
    assert len(cams) == 1
    # Media Foundation preferred over DirectShow.
    assert cams[0].index == 1400


@pytest.mark.skipif(sys.platform != "win32", reason="dedupe path is Windows-only")
def test_two_identical_models_are_numbered(monkeypatch) -> None:
    """Genuinely different devices with the same product name must stay
    distinguishable, or the dedupe would just move the confusion."""
    fake = [
        _FakeCam(index=1400, name="C920", path=r"\\?\usb#a#{guid-a}\global"),
        _FakeCam(index=1401, name="C920", path=r"\\?\usb#b#{guid-b}\global"),
    ]
    monkeypatch.setitem(
        sys.modules,
        "cv2_enumerate_cameras",
        type(sys)("cv2_enumerate_cameras"),
    )
    sys.modules["cv2_enumerate_cameras"].enumerate_cameras = lambda: fake

    cams = enumerate_cameras()
    assert len(cams) == 2
    assert {c.name for c in cams} == {"C920 #1", "C920 #2"}


def test_label_does_not_lead_with_backend_encoded_index() -> None:
    info = CameraInfo(index=1400, name="USB Video Device", backend="Media Foundation")
    assert not info.display_label.startswith("1400")
    assert "USB Video Device" in info.display_label


def test_label_falls_back_to_index_when_unnamed() -> None:
    assert CameraInfo(index=3, name="").display_label == "Camera 3"


def test_enumerate_never_raises() -> None:
    """Contract relied on by the wizard and main window."""
    assert isinstance(camera_mod.enumerate_cameras(), list)
