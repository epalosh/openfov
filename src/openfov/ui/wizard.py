"""First-run setup wizard.

Four-and-a-half steps; designed to take a brand-new user from "I just
installed OpenFOV" to "I'm tracking and I know where everything is" in
under a minute.

Pages:
  1. Welcome
  2. Choose an input source (camera)
  3. Calibrate the neutral pose
  4. Pick an output destination
  5. All set

The wizard is launched on first run and is re-runnable from the Help
menu. Outputs (chosen camera, neutral pose, game id) land on public
attributes after the user accepts.

Performance notes
-----------------
The previous build did camera enumeration + MediaPipe init + camera
open synchronously inside `initializePage()`. That made the Next
button appear to hang for a few seconds. This module defers the heavy
work to a 0-delay QTimer so each page transition is instantaneous and
the work loads with the user already looking at the destination page.
"""

from __future__ import annotations

import contextlib
import logging

import cv2
import numpy as np
from PySide6.QtCore import Qt, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
    QWizard,
    QWizardPage,
)

from openfov.persistence.config import AppConfig
from openfov.runtime.camera import CameraInfo, enumerate_cameras
from openfov.tracker.base import Pose6DOF
from openfov.tracker.mediapipe_tracker import MediaPipeTracker, TrackerSettings

logger = logging.getLogger(__name__)


# Palette aliases — keep in sync with resources/ui/openfov.qss.
_DIM = "#7a838c"


# ---------------------------------------------------------------------------
# Background workers for slow camera + MediaPipe init.
# ---------------------------------------------------------------------------
#
# Why threads: `cv2.VideoCapture(index, backend)` on Windows can stall the
# calling thread for 500-2000 ms while MSMF/DShow negotiate with the
# device. `cap.set()` adds another 50-200 ms. `MediaPipeTracker.start()`
# loads a TF Lite model and configures XNNPACK delegates — another 200-
# 500 ms first-time. Doing any of this on the Qt main thread freezes
# the entire UI (button clicks queue, the spinner can't repaint, the
# wizard page transition appears to hang).
#
# We push all three into QThread workers. Main thread stays responsive,
# the preview widget shows a "Connecting..." message, and the actual
# transition to live video happens via signal when the worker reports
# back. Cancellation is handled by tracking the most-recently-wanted
# camera index — stale worker results get their VideoCapture released
# without ever being shown.


class _CameraOpenWorker(QThread):
    """Opens a `cv2.VideoCapture` on a background thread.

    Tries the same backend chain as the production CameraSource (ANY →
    MSMF → DShow) and applies the standard resolution + buffer-size
    properties before reporting back. Emits the index alongside the cap
    so a fast-switching user's stale workers can be detected and
    discarded by the receiver."""

    opened = Signal(int, object)  # (camera_index, cv2.VideoCapture or None)

    def __init__(
        self,
        index: int,
        width: int = 1280,
        height: int = 720,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._index = index
        self._width = width
        self._height = height

    def run(self) -> None:
        cap: cv2.VideoCapture | None = None
        for backend in (cv2.CAP_ANY, cv2.CAP_MSMF, cv2.CAP_DSHOW):
            try:
                c = cv2.VideoCapture(self._index, backend)
            except cv2.error:
                continue
            if c.isOpened():
                cap = c
                break
            with contextlib.suppress(cv2.error):
                c.release()
        if cap is not None:
            try:
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, self._width)
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self._height)
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            except cv2.error as exc:
                logger.warning("cap.set raised in worker: %s", exc)
        self.opened.emit(self._index, cap)


class _TrackerInitWorker(QThread):
    """Initializes a MediaPipeTracker on a background thread.

    The first `start()` call loads `face_landmarker.task` and warms the
    XNNPACK delegate — 200-500 ms even on a fast CPU. Doing it inline
    in the Qt thread freezes the calibrate page during its transition."""

    ready = Signal(object)  # MediaPipeTracker or None on failure

    def run(self) -> None:
        try:
            t = MediaPipeTracker()
            t.start(TrackerSettings())
            self.ready.emit(t)
        except Exception as exc:
            logger.exception("Wizard tracker init failed: %s", exc)
            self.ready.emit(None)


# ---------------------------------------------------------------------------
# Live preview widget shared by the camera + calibrate pages.
# ---------------------------------------------------------------------------


class _LivePreview(QLabel):
    """Webcam preview backed by `cv2.VideoCapture`, running on a 33 ms
    QTimer. Optionally runs MediaPipe so the calibrate page can capture
    a real pose, and optionally draws green landmark crosses over the
    image.

    The slow parts of "open this camera" and "init MediaPipe" run on
    background `QThread`s so the UI stays responsive — even on the
    first-ever camera open where Windows can stall the calling thread
    for over a second.

    `pose_ready` carries `(Pose6DOF, detected, landmarks_2d | None)`.
    `camera_opened` carries `(camera_index, success)` so the page can
    update its status line without polling."""

    pose_ready = Signal(object, object, object)
    camera_opened = Signal(int, bool)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setAlignment(Qt.AlignCenter)
        self.setMinimumSize(520, 300)
        self.setMaximumSize(720, 405)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        # Self-contained dark background + thin border so the preview
        # reads as a distinct panel inside the wizard. No default text —
        # whichever page owns this widget is responsible for setting
        # an appropriate empty-state message (e.g. "Select a camera
        # above"). Saying "Loading camera..." before the user has
        # picked one is misleading.
        self.setStyleSheet(
            "background-color: #0a0d10; border: 1px solid #2c333b; border-radius: 6px;"
        )

        self._cap: cv2.VideoCapture | None = None
        self._timer = QTimer(self)
        self._timer.setInterval(33)  # ~30 fps
        self._timer.timeout.connect(self._tick)
        self._tracker: MediaPipeTracker | None = None
        self._tracker_enabled = False
        self._frame_index = 0
        self._draw_overlay = False
        self._landmarks: np.ndarray | None = None

        # Async-open state. `_wanted_index` is the camera the user most
        # recently asked for; stale worker results (a slower worker
        # finishing after the user changed their mind) are detected by
        # comparing the worker's index against this and discarded.
        self._wanted_index: int | None = None
        self._tracker_worker: _TrackerInitWorker | None = None

    def set_tracker_enabled(self, enabled: bool, draw_overlay: bool = False) -> None:
        """Enable MediaPipe inference. `draw_overlay=True` paints green
        landmark crosses on top of each frame. Tracker init runs on a
        background thread; until it finishes, frames render without
        detection (the calibrate page shows "no face detected" briefly,
        then live pose readings)."""
        self._draw_overlay = draw_overlay
        self._tracker_enabled = enabled
        if enabled and self._tracker is None and self._tracker_worker is None:
            worker = _TrackerInitWorker(parent=self)
            worker.ready.connect(self._on_tracker_ready, Qt.QueuedConnection)
            worker.finished.connect(worker.deleteLater)
            self._tracker_worker = worker
            worker.start()

    @Slot(object)
    def _on_tracker_ready(self, tracker: object) -> None:
        self._tracker_worker = None
        if tracker is None:
            logger.error(
                "Wizard tracker init returned None; calibrate page won't detect"
            )
            return
        # Honor a stop() that landed between worker start and result.
        if not self._tracker_enabled:
            try:
                tracker.stop()  # type: ignore[attr-defined]
            except Exception as exc:
                logger.debug("Discarded tracker stop raised: %s", exc)
            return
        self._tracker = tracker  # type: ignore[assignment]
        logger.info("Wizard tracker initialized OK")

    def open_camera(self, index: int) -> None:
        """Request opening the given camera index. Returns immediately;
        the actual `cv2.VideoCapture(...)` call runs on a worker thread
        and the preview transitions to live video via the
        `camera_opened` signal when ready. If the user picks a different
        camera before this one finishes, the stale result is discarded."""
        self._wanted_index = index
        # Release whatever camera we currently hold so the new worker
        # can claim the device without contention.
        self._close_camera_only()
        self.setText(f"Connecting to camera {index}…")

        worker = _CameraOpenWorker(index, parent=self)
        worker.opened.connect(self._on_open_done, Qt.QueuedConnection)
        worker.finished.connect(worker.deleteLater)
        worker.start()

    @Slot(int, object)
    def _on_open_done(self, idx: int, cap: object) -> None:
        # Stale-result check: the user may have changed their selection
        # while this worker was still spinning. Release the now-unwanted
        # VideoCapture and ignore.
        if idx != self._wanted_index:
            if cap is not None:
                try:
                    cap.release()  # type: ignore[attr-defined]
                except Exception as exc:
                    logger.debug("Discarded VideoCapture release raised: %s", exc)
            return
        if cap is None:
            self.setText(
                f"Could not open camera {idx}.\n"
                "Is another app (Zoom, OBS, Teams) using it?"
            )
            self.camera_opened.emit(idx, False)
            return
        self._cap = cap  # type: ignore[assignment]
        self._timer.start()
        self.camera_opened.emit(idx, True)

    def _close_camera_only(self) -> None:
        self._timer.stop()
        if self._cap is not None:
            try:
                self._cap.release()
            except cv2.error as exc:
                logger.debug("cap.release raised: %s", exc)
            self._cap = None

    def stop(self) -> None:
        """Full tear-down: camera, tracker, timer. Marks any in-flight
        worker result as unwanted so its produced VideoCapture is
        released rather than installed."""
        self._wanted_index = None
        self._close_camera_only()
        # Disable any pending tracker-init result.
        self._tracker_enabled = False
        if self._tracker is not None:
            try:
                self._tracker.stop()
            except Exception as exc:
                logger.debug("Tracker stop raised: %s", exc)
            self._tracker = None
        if self._frame_index > 0:
            self._frame_index = 0
        self._landmarks = None

    def _tick(self) -> None:
        if self._cap is None:
            return
        try:
            ok, frame = self._cap.read()
        except cv2.error:
            return
        if not ok or frame is None:
            return
        frame = cv2.flip(frame, 1)

        if self._tracker_enabled and self._tracker is not None:
            self._frame_index += 1
            try:
                result = self._tracker.step(frame, self._frame_index * 33)
                self._landmarks = result.landmarks_2d
                self.pose_ready.emit(result.pose, result.detected, result.landmarks_2d)
            except Exception as exc:
                logger.debug("Tracker step raised: %s", exc)
        else:
            self._landmarks = None

        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        h, w, _ = rgb.shape
        img = QImage(rgb.data, w, h, 3 * w, QImage.Format_RGB888).copy()
        pix = QPixmap.fromImage(img).scaled(
            self.width(), self.height(), Qt.KeepAspectRatio, Qt.FastTransformation
        )

        if self._draw_overlay and self._landmarks is not None and len(self._landmarks):
            painter = QPainter(pix)
            try:
                painter.setRenderHint(QPainter.Antialiasing, True)
                pen = QPen(QColor(60, 230, 90))
                pen.setWidth(2)
                painter.setPen(pen)
                scale_x = pix.width() / w
                scale_y = pix.height() / h
                for lx, ly in self._landmarks:
                    px = lx * scale_x
                    py = ly * scale_y
                    painter.drawLine(px - 3, py, px + 3, py)
                    painter.drawLine(px, py - 3, px, py + 3)
            finally:
                painter.end()

        self.setPixmap(pix)

    def __del__(self) -> None:
        with contextlib.suppress(Exception):
            self.stop()


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------


def _body_label(text: str, *, dim: bool = False) -> QLabel:
    """Standard body-text label used across pages — rich text + wrap on,
    optional muted color for footnotes."""
    lbl = QLabel(text)
    lbl.setWordWrap(True)
    lbl.setTextFormat(Qt.RichText)
    if dim:
        lbl.setStyleSheet(f"color: {_DIM};")
    return lbl


class _WelcomePage(QWizardPage):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setTitle("Welcome to OpenFOV!")

        body = _body_label(
            "OpenFOV uses your webcam to track your head, and feed that "
            "movement into iRacing."
            "<br><br>"
            "In the next few screens we'll:"
            "<ol style='margin-left: 8px; line-height: 150%;'>"
            "<li>Select an input source</li>"
            "<li>Calibrate your 'looking forward' pose</li>"
            "<li>Provide access to our full suite of features!</li>"
            "</ol>"
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 16, 24, 16)
        layout.setSpacing(10)
        layout.addWidget(body)
        layout.addStretch(1)


class _CameraPage(QWizardPage):
    # Placeholder shown at combo index 0 until the user makes an
    # affirmative choice. Its userData is None, which keeps `currentData`
    # falsy and `isComplete()` False so the Next button stays disabled.
    _PLACEHOLDER_LABEL = "— Select a camera —"

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setTitle("Choose an input source")
        self.setSubTitle(
            "Pick a webcam below and confirm you can see yourself in the preview."
        )

        self._combo = QComboBox()
        self._combo.setMinimumWidth(280)
        self._combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        cam_label = QLabel("Camera:")
        cam_label.setMinimumWidth(70)
        cam_row = QHBoxLayout()
        cam_row.setContentsMargins(0, 0, 0, 0)
        cam_row.addWidget(cam_label)
        cam_row.addWidget(self._combo, stretch=1)

        self._preview = _LivePreview()
        self._preview.camera_opened.connect(self._on_camera_opened)
        self._status = QLabel("")
        self._status.setStyleSheet(f"color: {_DIM};")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 16, 24, 16)
        layout.setSpacing(12)
        layout.addLayout(cam_row)
        layout.addWidget(self._preview, stretch=1)
        layout.addWidget(self._status)

        self._combo.currentIndexChanged.connect(self._on_selection_changed)
        self.registerField("camera_index*", self._combo, "currentData")
        self._populated = False
        # Track the last-selected camera so back-navigation can re-open
        # the preview without the user having to reselect.
        self._last_selected: int | None = None

    def initializePage(self) -> None:
        # Page transition first; camera enumeration + first open happen
        # one event-loop tick later so Next feels instantaneous.
        QTimer.singleShot(0, self._populate_and_open)

    def _populate_and_open(self) -> None:
        if not self._populated:
            self._populated = True
            self._combo.clear()
            # Index 0 is the "no choice yet" placeholder. Its userData
            # is None — currentData() returns None — isComplete() False
            # — Next stays disabled until the user picks a real camera.
            self._combo.addItem(self._PLACEHOLDER_LABEL, None)
            cams = enumerate_cameras() or [CameraInfo(index=0, name="Camera 0")]
            for cam in cams:
                self._combo.addItem(cam.display_label, cam.index)
            # Start on the placeholder. We don't auto-select the saved
            # camera here — the user should affirmatively pick, even on
            # a re-run of the wizard, so Next never enables without a
            # deliberate choice.
            self._combo.setCurrentIndex(0)
            self._status.setText("Pick a camera above to see the live preview.")
            # Set the empty-state message *inside* the preview pane too
            # so the big dark rectangle reads as intentional rather than
            # broken. setText replaces any pixmap.
            self._preview.setText("Select a camera above to see the live preview.")
        self._on_selection_changed(self._combo.currentIndex())

    def validatePage(self) -> bool:
        """Called when the user clicks Next. Release the VideoCapture
        immediately so the next page can claim the same physical camera
        without OpenCV's per-backend retry stall (the prior lag source).

        Returns True so the wizard advances. False would block."""
        # Remember which camera was selected so showEvent can restore
        # the preview on back-navigation.
        cam_index = self._combo.currentData()
        if cam_index is not None:
            self._last_selected = int(cam_index)
        self._preview.stop()
        return True

    def cleanupPage(self) -> None:
        # Called when the user clicks Back from a *later* page. Same
        # cleanup applies — release the device. showEvent will re-open
        # if we return here.
        self._preview.stop()

    def showEvent(self, event) -> None:
        """Re-open the camera preview when the page becomes visible
        again after the user navigated back. validatePage() and
        cleanupPage() both stopped the capture; we need to re-establish
        it without making the user fiddle with the combo."""
        super().showEvent(event)
        if self._populated and self._last_selected is not None:
            # Defer so the page is painted first.
            cam_index = self._last_selected
            QTimer.singleShot(0, lambda: self._do_open(cam_index))

    def _on_selection_changed(self, _idx: int) -> None:
        cam_index = self._combo.currentData()
        # Tell the wizard the page's completeness may have changed so
        # the Next button enables/disables correctly. The `*` field
        # mandatory marker doesn't auto-fire on combo populate, so we
        # nudge it manually.
        self.completeChanged.emit()
        if cam_index is None:
            # User is back on the "Select a camera" placeholder (either
            # initial state, or they reverted from a real choice).
            # Stop the live preview and put the empty-state hint back.
            self._preview.stop()
            self._preview.setText("Select a camera above to see the live preview.")
            self._status.setText("Pick a camera above to see the live preview.")
            return
        self._status.setText(f"Opening camera {cam_index}...")
        QTimer.singleShot(0, lambda: self._do_open(int(cam_index)))

    def _do_open(self, cam_index: int) -> None:
        """Kick off an async open. The status line and `_last_selected`
        are updated by `_on_camera_opened` when the worker reports back."""
        self._preview.open_camera(cam_index)
        self._status.setText(f"Connecting to camera {cam_index}…")

    @Slot(int, bool)
    def _on_camera_opened(self, cam_index: int, ok: bool) -> None:
        if ok:
            self._status.setText(f"Connected to camera {cam_index}.")
            self._last_selected = cam_index
        else:
            self._status.setText(f"Failed to open camera {cam_index}.")

    def isComplete(self) -> bool:
        return self._combo.currentData() is not None


class _CalibratePage(QWizardPage):
    """Look straight at the monitor, click Calibrate — the next reading
    becomes the user's neutral pose."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setTitle("Calibrate")
        self.setSubTitle(
            "Look straight at the center of your monitor and click "
            "Calibrate. This becomes your zero-degree reference."
        )

        # Live preview with landmark overlay drawn on the image itself.
        self._preview = _LivePreview()
        self._preview.pose_ready.connect(self._on_pose)

        # Numeric readout below the preview.
        self._readout = QLabel("waiting for face...")
        readout_font = self._readout.font()
        readout_font.setFamily("Consolas")
        readout_font.setPointSize(11)
        self._readout.setFont(readout_font)
        self._readout.setAlignment(Qt.AlignCenter)
        self._readout.setStyleSheet(
            "padding: 6px 10px; background-color: #1a1f25; "
            "border: 1px solid #2c333b; border-radius: 4px; color: #d6dbe1;"
        )

        # Calibrate button + status footer.
        self._calibrate_btn = QPushButton("Calibrate")
        self._calibrate_btn.setEnabled(False)
        self._calibrate_btn.setMinimumHeight(32)
        self._calibrate_btn.clicked.connect(self._on_capture)

        # Escape hatch. Calibration needs a detected face, and without this
        # the page is a hard dead end for anyone whose camera angle,
        # lighting or webcam placement stops MediaPipe finding them: the
        # Calibrate button stays disabled, so Next stays disabled, so the
        # only way out of first-run setup is Cancel. Skipping just leaves
        # the neutral pose at its default; the user can recenter with F9
        # or re-run calibration later from the Help menu.
        self._skip_btn = QPushButton("Skip for now")
        self._skip_btn.setMinimumHeight(32)
        self._skip_btn.setToolTip(
            "Continue without calibrating. You can set your neutral pose "
            "any time by pressing F9 while looking straight ahead."
        )
        self._skip_btn.clicked.connect(self._on_skip)

        self._status = QLabel("")
        self._status.setStyleSheet(f"color: {_DIM};")
        self._status.setWordWrap(True)

        action_row = QHBoxLayout()
        action_row.addWidget(self._calibrate_btn)
        action_row.addWidget(self._skip_btn)
        action_row.addSpacing(12)
        action_row.addWidget(self._status, stretch=1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 16, 24, 16)
        layout.setSpacing(10)
        layout.addWidget(self._preview, stretch=1)
        layout.addWidget(self._readout)
        layout.addLayout(action_row)

        self._latest_pose: Pose6DOF | None = None
        self._neutral_captured: Pose6DOF | None = None
        self._skipped = False

    def initializePage(self) -> None:
        cam_index = int(self.field("camera_index"))
        self._calibrate_btn.setEnabled(False)
        self._status.setText("")
        self._neutral_captured = None
        self._skipped = False
        # Page transition first; tracker init + camera open happen one
        # tick later so Next feels instantaneous.
        QTimer.singleShot(0, lambda: self._activate(cam_index))

    def _activate(self, cam_index: int) -> None:
        self._preview.set_tracker_enabled(True, draw_overlay=True)
        self._preview.open_camera(cam_index)

    def cleanupPage(self) -> None:
        self._preview.stop()

    def _on_pose(
        self, pose: Pose6DOF, detected: bool, _landmarks_2d: object
    ) -> None:
        if not detected:
            self._readout.setText("no face detected")
            self._latest_pose = None
            self._calibrate_btn.setEnabled(False)
            return
        self._latest_pose = pose
        self._readout.setText(
            f"yaw {pose.yaw:+6.2f}°    pitch {pose.pitch:+6.2f}°    "
            f"roll {pose.roll:+6.2f}°"
        )
        self._calibrate_btn.setEnabled(True)

    def _on_capture(self) -> None:
        if self._latest_pose is None:
            return
        self._neutral_captured = self._latest_pose
        self._skipped = False
        self._status.setText(
            f"Calibrated at ({self._latest_pose.yaw:+5.1f}°, "
            f"{self._latest_pose.pitch:+5.1f}°)."
        )
        self.completeChanged.emit()

    def _on_skip(self) -> None:
        self._skipped = True
        self._neutral_captured = None
        self._status.setText(
            "Skipped — press F9 while looking straight ahead to set your "
            "neutral pose later."
        )
        self.completeChanged.emit()

    def isComplete(self) -> bool:
        return self._neutral_captured is not None or self._skipped

    def neutral_pose(self) -> Pose6DOF | None:
        """None when the user skipped — callers must leave the existing
        neutral alone rather than treating it as a zeroed calibration."""
        return self._neutral_captured


class _HeadsUpPage(QWizardPage):
    """The reality-check page. Head tracking is genuinely different from
    a static camera view, and first-timers can bounce off if nobody warns
    them. We make them tick an oath checkbox before they can finish —
    a small ceremonial moment that asks for actual commitment.

    No page title or subtitle: the user explicitly wanted a clean page
    whose visual weight is the body copy and the oath checkbox."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)

        body = _body_label(
            "<b style='color:#cfeae5;'>IMPORTANT:</b> Head-tracking can "
            "feel disorienting at first. You've spent hundreds (maybe "
            "thousands!) of hours with a static view — you might spin "
            "a bit, and that's normal. <b>Stick with it:</b> after 2-3 "
            "sessions it clicks."
            "<ul style='margin-left: 8px; line-height: 160%;'>"
            "<li><b>Don't overuse it.</b> Keep your head still by "
            "default — only move when you need to (apex/exit, "
            "wheel-to-wheel battles, learning new tracks). Constant "
            "motion can be disorienting!</li>"
            "<li><b>Tune for your setup.</b> The defaults are a "
            "starting point — adjust sensitivity and filters until "
            "your movements feel right for your monitor distance.</li>"
            "</ul>"
        )

        # The oath. `isComplete()` watches this — Next stays disabled
        # until the user ticks it. registerField('*', ...) would also
        # work but the checkbox lives on a different page, and we want
        # the page's own isComplete to drive enablement so the field-
        # change machinery doesn't fire stale completeness checks on
        # sibling pages.
        self._oath = QCheckBox("I will not give up!")
        oath_font = self._oath.font()
        oath_font.setBold(True)
        oath_font.setPointSize(oath_font.pointSize() + 1)
        self._oath.setFont(oath_font)
        self._oath.toggled.connect(lambda _on: self.completeChanged.emit())

        # Top margin is intentionally large here. The page has no title /
        # subtitle, so the ModernStyle header area is empty — without this
        # extra padding the IMPORTANT lead-in sits right under the dialog
        # chrome and feels cramped.
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 56, 24, 16)
        layout.setSpacing(12)
        layout.addWidget(body)
        layout.addStretch(1)
        layout.addWidget(self._oath)

    def isComplete(self) -> bool:
        return self._oath.isChecked()


class _FinishPage(QWizardPage):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setTitle("All set — let's race!")
        self.setSubTitle(
            "OpenFOV is ready. Fire up iRacing and your head moves the camera."
        )

        body = _body_label(
            "<b>Quick things to know:</b>"
            "<ul style='margin-left: 8px; line-height: 160%;'>"
            "<li><b>iRacing should already have TrackIR turned on.</b> "
            "If your view doesn't move when you turn your head, jump "
            "into iRacing's <b>Options → Graphics</b> and tick "
            "<b>Enable TrackIR</b>. One quick iRacing restart and you're "
            "all set.</li>"
            "<li>Press <b>F9</b> at any time to recalibrate your view.</li>"
            "<li>OpenFOV lives in your system tray — closing the window "
            "keeps it running. Right-click the tray icon for Show / Quit.</li>"
            "<li>Want to revisit this setup later? "
            "<b>Help → Run setup wizard…</b> in the main window.</li>"
            "</ul>"
            "<br>"
            "Click <b>Finish</b> to start tracking. Have a blast out there!"
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 16, 24, 16)
        layout.setSpacing(10)
        layout.addWidget(body)
        layout.addStretch(1)


# ---------------------------------------------------------------------------
# The wizard itself.
# ---------------------------------------------------------------------------


class SetupWizard(QWizard):
    """First-run / re-runnable setup wizard."""

    def __init__(self, config: AppConfig, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("OpenFOV — Setup")
        # ModernStyle gives a clean header + content split that plays
        # well with our dark QSS. Windows' default AeroStyle doesn't.
        self.setWizardStyle(QWizard.ModernStyle)
        self.setOption(QWizard.NoBackButtonOnStartPage, True)
        self.setOption(QWizard.HaveHelpButton, False)
        self.setOption(QWizard.NoCancelButtonOnLastPage, True)
        self.setProperty("app_config", config)
        self.setMinimumSize(640, 540)

        self._welcome = _WelcomePage()
        self._camera = _CameraPage()
        self._calibrate = _CalibratePage()
        self._headsup = _HeadsUpPage()
        self._finish = _FinishPage()

        for page in (
            self._welcome,
            self._camera,
            self._calibrate,
            self._headsup,
            self._finish,
        ):
            self.addPage(page)

        # Outputs filled in on accept. The output destination is no
        # longer user-selectable in the wizard — iRacing is the only
        # supported game today, so we hardcode it. If we ever add a
        # second target we'll bring the picker back.
        self.chosen_camera_index: int = config.camera_index
        self.chosen_game_id: str = "iracing"
        self.neutral_pose: Pose6DOF | None = None

        self.accepted.connect(self._on_accept)
        self.rejected.connect(self._on_reject)

    def _on_accept(self) -> None:
        self._capture_camera_choice()
        # chosen_game_id is fixed at "iracing" — see __init__.
        self.neutral_pose = self._calibrate.neutral_pose()

    def _on_reject(self) -> None:
        """Cancelling drops the user into the main window rather than
        quitting, so a camera they already confirmed a live preview on is
        still the right one to use. Losing it left them staring at camera
        0, which is frequently not a webcam at all."""
        self._capture_camera_choice()

    def _capture_camera_choice(self) -> None:
        # userData is None while the "— Select a camera —" placeholder is
        # active, i.e. the user never made an affirmative choice.
        value = self.field("camera_index")
        if value is not None:
            self.chosen_camera_index = int(value)


__all__ = ["SetupWizard"]
