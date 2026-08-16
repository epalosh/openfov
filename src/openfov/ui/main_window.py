"""OpenFOV main window.

Wires together: profile bar (top) + camera view (left) + pose readout
(right) + axis panels (yaw, pitch, roll) + filter panel + buttons. Owns
the canonical `Profile` state and pushes changes to the pipeline thread.

Hides to tray on close (per design spec)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from PySide6.QtCore import QEvent, Qt, Signal, Slot
from PySide6.QtGui import QAction, QCloseEvent
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from openfov.filtering.pipeline import AxisFilterParams
from openfov.games.base import GameProfile
from openfov.mapping.axis_mapper import AxisSettings
from openfov.persistence.profiles import Profile, save_profile
from openfov.runtime.camera import CameraInfo, enumerate_cameras
from openfov.tracker.base import Pose6DOF
from openfov.ui.axis_panel import AxisPanel
from openfov.ui.camera_view import CameraView
from openfov.ui.filter_panel import FilterPanel
from openfov.ui.pose_readout import PoseReadout
from openfov.ui.pose_widget import PoseWidget
from openfov.ui.profile_bar import ProfileBar
from openfov.ui.resources import app_icon

if TYPE_CHECKING:
    from openfov.runtime.pipeline import PipelineStats, PipelineThread

logger = logging.getLogger(__name__)

_ROTATION_AXES: tuple[tuple[str, str], ...] = (
    ("yaw", "Yaw"),
    ("pitch", "Pitch"),
    ("roll", "Roll"),
)


class MainWindow(QMainWindow):
    """Main app window. Composes UI + signals."""

    # Signals out — the app wires these to the runtime.
    request_recenter = Signal()
    request_quit = Signal()
    request_camera_switch = Signal(int)
    request_settings = Signal()
    request_wizard = Signal()
    request_always_on_top = Signal(bool)

    def __init__(
        self,
        initial_profile: Profile,
        always_on_top: bool = False,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("OpenFOV")
        self.setWindowIcon(app_icon())
        self.resize(1080, 820)
        self._profile = initial_profile
        self._allow_close = False
        self._always_on_top_initial = always_on_top
        # Manual "Pause preview" toggle (a diagnostic to measure the
        # preview's CPU cost). Independent of window visibility gating.
        self._preview_paused = False

        # ------------------------------------------------------------------
        # Profile bar — moves *under* the camera preview so the profile
        # controls live with the thing they describe.
        # ------------------------------------------------------------------
        self._profile_bar = ProfileBar(initial_profile_name=initial_profile.name)
        self._profile_bar.profile_loaded.connect(self._on_profile_loaded)
        self._profile_bar.request_save.connect(self._on_save_current)

        # ------------------------------------------------------------------
        # Top row: camera selector + Calibrate button, side by side
        # above the preview.
        # ------------------------------------------------------------------
        self._camera_combo = QComboBox()
        self._camera_combo.setMinimumWidth(180)
        self._populate_cameras()
        self._camera_combo.currentIndexChanged.connect(self._on_camera_changed)

        self._btn_calibrate = QPushButton("Calibrate")
        self._btn_calibrate.setToolTip(
            "Capture your current head pose as the new zero-degree reference.\n"
            "Keyboard shortcut: F9."
        )
        self._btn_calibrate.clicked.connect(self.request_recenter.emit)

        # Diagnostic toggle: stop repainting the preview to see what the
        # camera view costs the tracking loop. Tracking + game output keep
        # running and the fps/inference readout keeps updating, so the
        # effect is visible immediately in the status bar.
        self._btn_pause_preview = QPushButton("Pause preview")
        self._btn_pause_preview.setCheckable(True)
        self._btn_pause_preview.setToolTip(
            "Stop repainting the camera preview to measure its CPU cost.\n"
            "Tracking and output to your game keep running; watch the fps /\n"
            "inference readout in the status bar respond."
        )
        self._btn_pause_preview.toggled.connect(self._on_pause_preview)

        camera_row = QHBoxLayout()
        camera_row.setContentsMargins(0, 0, 0, 0)
        camera_row.addWidget(QLabel("Camera:"))
        camera_row.addWidget(self._camera_combo, 1)
        camera_row.addSpacing(10)
        camera_row.addWidget(self._btn_calibrate)
        camera_row.addWidget(self._btn_pause_preview)

        # ------------------------------------------------------------------
        # Left column: camera dropdown -> preview -> profile bar
        # Right column: pose widget -> badge -> readout
        # ------------------------------------------------------------------
        self._camera_view = CameraView()
        self._pose_readout = PoseReadout()

        self._pose_widget = PoseWidget()
        self._pose_widget.setMinimumHeight(220)

        # Tiny "game detected" badge: dot + label, lives between the 3D
        # widget and the numeric readout.
        self._game_badge = QLabel("● no game detected")
        self._game_badge.setStyleSheet("color: #7a838c;")

        # "Game is running" and "game is actually reading our head tracking"
        # are very different claims, and conflating them is exactly how a
        # totally broken output path looked healthy for three releases. The
        # badge above reports process detection; this one reports the real
        # end-to-end link (see PipelineThread.client_status).
        self._link_badge = QLabel("○ no game is reading head tracking")
        self._link_badge.setStyleSheet("color: #7a838c;")
        self._link_badge.setWordWrap(True)
        self._client_connected = False

        left_col = QVBoxLayout()
        left_col.setSpacing(6)
        left_col.addLayout(camera_row)
        left_col.addWidget(self._camera_view, 1)
        left_col.addWidget(self._profile_bar)

        left_col_widget = QWidget()
        left_col_widget.setLayout(left_col)

        right_col = QVBoxLayout()
        right_col.addWidget(self._pose_widget)
        right_col.addWidget(self._game_badge)
        right_col.addWidget(self._link_badge)
        right_col.addWidget(self._pose_readout, 1)

        right_col_widget = QWidget()
        right_col_widget.setLayout(right_col)
        right_col_widget.setMinimumWidth(280)
        right_col_widget.setMaximumWidth(340)

        split = QHBoxLayout()
        split.addWidget(left_col_widget, 1)
        split.addWidget(right_col_widget)

        # ------------------------------------------------------------------
        # Axis panels (yaw / pitch / roll)
        # ------------------------------------------------------------------
        self._axis_panels: dict[str, AxisPanel] = {}
        axis_grid = QGridLayout()
        axis_grid.setContentsMargins(0, 0, 0, 0)
        axis_grid.setHorizontalSpacing(8)
        for col, (axis, label) in enumerate(_ROTATION_AXES):
            panel = AxisPanel(axis_name=axis, label=label)
            panel.changed.connect(self._on_axis_changed)
            self._axis_panels[axis] = panel
            axis_grid.addWidget(panel, 0, col)

        # ------------------------------------------------------------------
        # Filter panel
        # ------------------------------------------------------------------
        self._filter_panel = FilterPanel()
        self._filter_panel.changed.connect(self._on_filter_changed)

        # ------------------------------------------------------------------
        # Compose
        # ------------------------------------------------------------------
        outer = QVBoxLayout()
        outer.setContentsMargins(12, 10, 12, 10)
        outer.setSpacing(10)
        outer.addLayout(split, 1)
        outer.addLayout(axis_grid)
        outer.addWidget(self._filter_panel)

        scroll_inner = QWidget()
        scroll_inner.setLayout(outer)

        scroll = QScrollArea()
        scroll.setWidget(scroll_inner)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        self.setCentralWidget(scroll)

        # Status bar.
        sb = QStatusBar()
        sb.showMessage("ready")
        self.setStatusBar(sb)

        # Menu bar.
        self._build_menus()

        # Push the initial profile's settings into the widgets without
        # firing change signals back at us.
        self._apply_profile_to_ui(initial_profile)

    # ------------------------------------------------------------------
    # Menus
    # ------------------------------------------------------------------

    def _build_menus(self) -> None:
        mb = self.menuBar()

        # File.
        m_file = mb.addMenu("&File")
        act_settings = QAction("&Settings...", self)
        act_settings.setShortcut("Ctrl+,")
        act_settings.triggered.connect(self.request_settings.emit)
        m_file.addAction(act_settings)
        m_file.addSeparator()
        act_quit = QAction("&Quit", self)
        act_quit.setShortcut("Ctrl+Q")
        act_quit.triggered.connect(self.request_quit.emit)
        m_file.addAction(act_quit)

        # Profile.
        m_prof = mb.addMenu("&Profile")
        act_save = QAction("&Save", self)
        act_save.setShortcut("Ctrl+S")
        act_save.triggered.connect(self._on_save_current)
        m_prof.addAction(act_save)
        # Save-As / Rename / Delete are owned by ProfileBar; the menu acts
        # as a shortcut hub. We invoke the bar's slots directly.
        act_save_as = QAction("Save &as...", self)
        act_save_as.setShortcut("Ctrl+Shift+S")
        act_save_as.triggered.connect(self._profile_bar._on_save_as)
        m_prof.addAction(act_save_as)
        act_rename = QAction("&Rename...", self)
        act_rename.triggered.connect(self._profile_bar._on_rename)
        m_prof.addAction(act_rename)
        act_delete = QAction("&Delete", self)
        act_delete.triggered.connect(self._profile_bar._on_delete)
        m_prof.addAction(act_delete)

        # View.
        m_view = mb.addMenu("&View")
        self._act_always_on_top = QAction("Always on &top", self, checkable=True)
        self._act_always_on_top.setChecked(self._always_on_top_initial)
        if self._always_on_top_initial:
            self.setWindowFlags(self.windowFlags() | Qt.WindowStaysOnTopHint)
        self._act_always_on_top.toggled.connect(self._on_always_on_top)
        m_view.addAction(self._act_always_on_top)

        # Help.
        m_help = mb.addMenu("&Help")
        act_wizard = QAction("Run &setup wizard...", self)
        act_wizard.triggered.connect(self.request_wizard.emit)
        m_help.addAction(act_wizard)
        m_help.addSeparator()
        act_about = QAction("&About OpenFOV", self)
        act_about.triggered.connect(self._show_about)
        m_help.addAction(act_about)

    @Slot(bool)
    def _on_always_on_top(self, on: bool) -> None:
        flags = self.windowFlags()
        if on:
            self.setWindowFlags(flags | Qt.WindowStaysOnTopHint)
        else:
            self.setWindowFlags(flags & ~Qt.WindowStaysOnTopHint)
        self.show()  # toggling flags requires re-show
        self.request_always_on_top.emit(on)

    def _show_about(self) -> None:
        import openfov

        QMessageBox.about(
            self, "About OpenFOV",
            f"<h3>OpenFOV {openfov.__version__}</h3>"
            "<p>Webcam head tracking for iRacing and other sim games.</p>"
            "<p>Bundled NPClient stub + FreeTrack shared memory handle "
            "the game-side delivery.</p>"
            "<p>MIT licensed - "
            "<a href=\"https://github.com/epalosh/openfov\">"
            "github.com/epalosh/openfov</a></p>",
        )

    # ------------------------------------------------------------------
    # Pipeline hookup (called by the entry point after construction)
    # ------------------------------------------------------------------

    def attach_pipeline(self, pipeline: PipelineThread) -> None:
        """Wire pipeline signals to UI slots, and UI signals to pipeline
        thread-safe control methods."""
        # Inbound
        pipeline.frame_ready.connect(self._camera_view.update_frame, Qt.QueuedConnection)
        pipeline.pose_ready.connect(self._pose_readout.update_pose, Qt.QueuedConnection)
        pipeline.pose_ready.connect(self._pose_widget.update_pose, Qt.QueuedConnection)
        pipeline.pose_ready.connect(self._on_pose, Qt.QueuedConnection)
        pipeline.camera_status.connect(self._on_camera_status, Qt.QueuedConnection)
        pipeline.error.connect(self._on_pipeline_error, Qt.QueuedConnection)
        pipeline.client_status.connect(self._on_client_status, Qt.QueuedConnection)
        # Outbound
        self.request_recenter.connect(pipeline.request_recenter)
        self.request_camera_switch.connect(pipeline.set_camera_index)
        # Reapply current settings now that the pipeline exists.
        self._push_profile_to_pipeline(pipeline)
        self._pipeline = pipeline  # type: ignore[attr-defined]
        # Seed the pipeline's UI-active state to match our current
        # visibility. We're not shown yet at attach time, so this starts
        # False; the showEvent fired by window.show() flips it True.
        self._sync_pipeline_ui_active()
        pipeline.set_preview_active(not self._preview_paused)

    # ------------------------------------------------------------------
    # Profile sync
    # ------------------------------------------------------------------

    def _apply_profile_to_ui(self, profile: Profile) -> None:
        for axis, panel in self._axis_panels.items():
            panel.set_settings(profile.axes[axis])
        for axis, params in profile.filters.items():
            if axis in ("yaw", "pitch", "roll"):
                self._filter_panel.set_params(axis, params)

    def _push_profile_to_pipeline(self, pipeline: PipelineThread) -> None:
        for axis, settings in self._profile.axes.items():
            pipeline.update_axis_settings(axis, settings)
        for axis, params in self._profile.filters.items():
            pipeline.update_filter_params(axis, params)

    @Slot(object)
    def _on_profile_loaded(self, profile: Profile) -> None:
        self._profile = profile
        self._apply_profile_to_ui(profile)
        if hasattr(self, "_pipeline"):
            self._push_profile_to_pipeline(self._pipeline)
        self.statusBar().showMessage(f"loaded profile '{profile.name}'", 3000)

    @Slot()
    def _on_save_current(self) -> None:
        try:
            self._profile.name = self._profile_bar.current_name()
            save_profile(self._profile)
            self.statusBar().showMessage(f"saved '{self._profile.name}'", 3000)
        except Exception as exc:
            logger.exception("Save failed: %s", exc)
            self.statusBar().showMessage(f"save failed: {exc}", 5000)

    # ------------------------------------------------------------------
    # Settings change slots
    # ------------------------------------------------------------------

    @Slot(str, object)
    def _on_axis_changed(self, axis: str, settings: AxisSettings) -> None:
        self._profile.axes[axis] = settings
        if hasattr(self, "_pipeline"):
            self._pipeline.update_axis_settings(axis, settings)

    @Slot(str, object)
    def _on_filter_changed(self, axis: str, params: AxisFilterParams) -> None:
        self._profile.filters[axis] = params
        if hasattr(self, "_pipeline"):
            self._pipeline.update_filter_params(axis, params)

    # ------------------------------------------------------------------
    # Pose / status hooks
    # ------------------------------------------------------------------

    @Slot(object, object, object)
    def _on_pose(
        self, raw: Pose6DOF, mapped: Pose6DOF, stats: PipelineStats
    ) -> None:
        # Keep the preview's banner in sync with the hotkey on/off state so
        # a disabled, still-live preview doesn't read as "no face detected".
        self._camera_view.set_tracking_disabled(not stats.tracking_enabled)

        # Push the live dot into each axis's curve editor — raw input vs
        # mapped output, both in degrees. The mapper applies sensitivity
        # *before* the curve, so the dot's x corresponds to the curve's
        # input domain after we factor sensitivity back in.
        if stats.detected and stats.camera_connected:
            for axis_name, panel in self._axis_panels.items():
                raw_v = getattr(raw, axis_name)
                # The mapper does: x -> invert? -> *sens -> curve(x) -> clamp.
                # The curve sees `invert? * sens * raw_v`, so that's the
                # x we should show on the editor.
                ax_settings = panel.settings()
                input_to_curve = (-raw_v if ax_settings.invert else raw_v) * ax_settings.sensitivity
                output = getattr(mapped, axis_name)
                panel.set_live(input_to_curve, output)
        else:
            for panel in self._axis_panels.values():
                panel.clear_live()

        if not stats.camera_connected:
            # The camera_status slot owns the disconnected message; here
            # just show fps/inference status (which is 0/0 when disconnected).
            self.statusBar().showMessage("camera offline")
        elif not stats.tracking_enabled:
            self.statusBar().showMessage(
                "tracking disabled — view centered (press the toggle hotkey to resume)"
            )
        else:
            self.statusBar().showMessage(
                f"{stats.fps:4.1f} fps  •  {stats.inference_ms:.1f} ms  •  "
                f"{'tracking' if stats.detected else 'searching'}"
            )

    @Slot(bool, str)
    def _on_camera_status(self, connected: bool, message: str) -> None:
        # Persist disconnect messages longer so users notice; reconnect
        # messages are reassuring and short-lived.
        timeout = 0 if not connected else 4000
        self.statusBar().showMessage(message, timeout)

    @Slot(str)
    def _on_pipeline_error(self, message: str) -> None:
        self.statusBar().showMessage(message, 10000)

    @Slot(object)
    def on_game_changed(self, profile: GameProfile | None) -> None:
        """Slot for GameWatcher.game_changed. Updates the badge + routes
        the new output profile to the pipeline.

        Note this only means the game's *process* is running. Whether it is
        reading our head tracking is a separate question, answered by
        `_on_client_status`."""
        self._detected_game = profile
        if profile is None:
            self._game_badge.setText("● no game detected")
            self._game_badge.setStyleSheet("color: #7a838c;")
            self.statusBar().showMessage("game not running", 3000)
        else:
            self._game_badge.setText(f"● {profile.display_name} detected")
            self._game_badge.setStyleSheet("color: #82c4ae;")
            self.statusBar().showMessage(f"{profile.display_name} started", 3000)
            if hasattr(self, "_pipeline"):
                self._pipeline.set_game_output(
                    profile.output,
                    requires_trackir_process=profile.requires_trackir_process,
                )
        self._refresh_link_badge()

    @Slot(bool, int)
    def _on_client_status(self, connected: bool, game_id: int) -> None:
        """Slot for PipelineThread.client_status — a game really did load
        our NPClient and register itself."""
        self._client_connected = connected
        self._client_game_id = game_id
        self._refresh_link_badge()
        if connected:
            self.statusBar().showMessage("head tracking connected to game", 4000)

    def _refresh_link_badge(self) -> None:
        """Three distinguishable states, because 'nothing is happening' used
        to be indistinguishable from 'working'."""
        game = getattr(self, "_detected_game", None)
        if self._client_connected:
            name = game.display_name if game else "A game"
            self._link_badge.setText(f"● {name} is reading head tracking")
            self._link_badge.setStyleSheet("color: #82c4ae;")
        elif game is not None:
            # The interesting failure: the game is up but has not loaded
            # our client. Say so plainly instead of implying success.
            self._link_badge.setText(
                f"▲ {game.display_name} is running but is not reading head "
                f"tracking yet — restart the game if this persists"
            )
            self._link_badge.setStyleSheet("color: #d9a441;")
        else:
            self._link_badge.setText("○ no game is reading head tracking")
            self._link_badge.setStyleSheet("color: #7a838c;")

    # ------------------------------------------------------------------
    # Camera dropdown
    # ------------------------------------------------------------------

    def _populate_cameras(self) -> None:
        self._camera_combo.blockSignals(True)
        try:
            self._camera_combo.clear()
            cams = enumerate_cameras()
            if not cams:
                # Generic fallback so the combo isn't empty.
                cams = [CameraInfo(index=0, name="Camera 0")]
            for cam in cams:
                self._camera_combo.addItem(cam.display_label, cam.index)
        finally:
            self._camera_combo.blockSignals(False)

    @Slot(int)
    def _on_camera_changed(self, _combo_idx: int) -> None:
        idx = self._camera_combo.currentData()
        if idx is None:
            return
        self.request_camera_switch.emit(int(idx))
        self.statusBar().showMessage(f"switched to camera {idx}", 3000)

    # ------------------------------------------------------------------
    # Close = quit. The X button means what it says.
    # ------------------------------------------------------------------
    # The shutdown chain in __main__ stops the pipeline thread, persists
    # state, releases the single-instance lock, and *then* re-closes the
    # window with `_allow_close = True` — second call here accepts the
    # event and the QApplication exits.

    # ------------------------------------------------------------------
    # Visibility → pipeline UI-load gating
    # ------------------------------------------------------------------
    # When the window is minimized or hidden to tray, the pipeline has no
    # reason to ship preview frames or pose updates. We translate Qt
    # show/hide/minimize transitions into pipeline.set_ui_active() so the
    # tracking loop sheds that UI load exactly while the game is in front.

    def _sync_pipeline_ui_active(self) -> None:
        pipeline = getattr(self, "_pipeline", None)
        if pipeline is None:
            return
        # A minimized window is still "visible" in Qt's sense, so check
        # both: active means on-screen AND not minimized.
        pipeline.set_ui_active(self.isVisible() and not self.isMinimized())

    @Slot(bool)
    def _on_pause_preview(self, paused: bool) -> None:
        self._preview_paused = paused
        self._btn_pause_preview.setText("Resume preview" if paused else "Pause preview")
        if hasattr(self, "_pipeline"):
            self._pipeline.set_preview_active(not paused)
        if paused:
            self._camera_view.show_idle("preview paused\ntracking + output still running")
        self.statusBar().showMessage(
            "preview paused - tracking still running" if paused else "preview resumed",
            3000,
        )

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.WindowStateChange:
            self._sync_pipeline_ui_active()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._sync_pipeline_ui_active()

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        self._sync_pipeline_ui_active()

    def allow_close(self) -> None:
        self._allow_close = True

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._allow_close:
            event.accept()
            return
        # First close click — kick off the shutdown chain and swallow
        # this event. The chain will re-call close() once cleanup is done.
        event.ignore()
        self.request_quit.emit()


__all__ = ["MainWindow"]
