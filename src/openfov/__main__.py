"""OpenFOV entry point.

Three modes:
- `python -m openfov`                — Qt GUI (default).
- `python -m openfov --headless`     — pipeline only, prints pose, no UI.
- `python -m openfov --debug-tracker` — sine-wave generator (no camera).
  Implies --headless. Runs on any OS; useful for CI smoke tests."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from dataclasses import replace

logger = logging.getLogger("openfov")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="openfov", description="OpenFOV head tracker")
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run the data pipeline in console mode (no Qt UI).",
    )
    parser.add_argument(
        "--debug-tracker",
        action="store_true",
        help="Use the debug sine-wave tracker instead of MediaPipe. Implies --headless.",
    )
    parser.add_argument(
        "--camera-index",
        type=int,
        default=None,
        help="OpenCV camera index. Default: read from config.",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=0.0,
        help="If >0, run for this many seconds then exit (useful for CI).",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args(argv)

    log_level = getattr(logging, args.log_level)
    log_format = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    logging.basicConfig(level=log_level, format=log_format)

    # Also write logs to a rotating file under %APPDATA%\OpenFOV\.
    # Critical for end-user support: the Nuitka GUI build runs with
    # --windows-console-mode=disable, so stderr goes nowhere. Without
    # this file handler users have no diagnostic output to attach to
    # bug reports. 2 MB x 3 = 6 MB max on disk; rolls automatically.
    try:
        from logging.handlers import RotatingFileHandler

        from openfov.persistence.paths import app_data_dir

        log_dir = app_data_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "openfov.log"
        file_handler = RotatingFileHandler(
            log_path,
            maxBytes=2 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        file_handler.setLevel(log_level)
        file_handler.setFormatter(logging.Formatter(log_format))
        logging.getLogger().addHandler(file_handler)
        logger.info("Log file: %s", log_path)
    except Exception as exc:
        # Logging is best-effort. If %APPDATA% is unavailable (rare —
        # locked profiles, sandbox environments) we degrade to stderr
        # only and keep running rather than refusing to launch.
        logger.warning("Could not enable file logging: %s", exc)

    if args.headless or args.debug_tracker:
        return _run_headless(
            use_debug=args.debug_tracker,
            camera_index=args.camera_index or 0,
            duration=args.duration,
        )

    return _run_gui(camera_index=args.camera_index)


# ----------------------------------------------------------------------
# GUI entry
# ----------------------------------------------------------------------


def _run_gui(camera_index: int | None) -> int:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QMessageBox

    from openfov.games import BUILTIN_PROFILES, get_profile
    from openfov.output.manager import OutputManager
    from openfov.persistence.config import load_app_config, save_app_config
    from openfov.persistence.profiles import load_profile, save_profile
    from openfov.runtime.game_watcher import GameWatcher
    from openfov.runtime.hotkey import GlobalHotkey
    from openfov.runtime.pipeline import PipelineThread
    from openfov.runtime.single_instance import SingleInstanceLock
    from openfov.tracker.mediapipe_tracker import MediaPipeTracker
    from openfov.ui.main_window import MainWindow
    from openfov.ui.resources import app_icon, load_stylesheet
    from openfov.ui.settings_dialog import SettingsDialog
    from openfov.ui.tray import Tray
    from openfov.ui.wizard import SetupWizard

    # Tell Windows we're a distinct application, not a generic Python
    # interpreter. Without this, the taskbar groups us with other Python
    # processes and the system-tray icon falls back to python.exe's icon
    # instead of the QIcon we'd otherwise set. Set it *before* the
    # QApplication is created so Windows associates the AUMID with the
    # very first window we open. No-op on non-Windows. The Nuitka build
    # gets this benefit too — pinned-to-taskbar, jumplists, and the
    # tray icon all key off this string.
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "OpenFOV.Tracker.1"
            )
        except (OSError, AttributeError) as exc:
            logger.debug("Could not set AppUserModelID: %s", exc)

        # Bump our process priority class to HIGH so our pipeline +
        # camera-reader threads can hold their schedule under iRacing's
        # render load. Fullscreen games typically auto-promote themselves
        # to HIGH; our threads being ABOVE_NORMAL inside a NORMAL-class
        # process still lose contests against iRacing's threads inside
        # a HIGH-class process. Matching iRacing's process class fixes
        # the "44 fps under load, 60 fps idle" pattern.
        #
        # Safe: HIGH is below REALTIME (which can starve Windows itself)
        # and equal to what most fullscreen games already use. Worst case
        # is slightly more CPU heat during sessions — the kernel + audio
        # services still get priority over us.
        try:
            # Use psutil (already a dependency) instead of raw ctypes. The
            # previous ctypes path called SetPriorityClass without setting
            # restype/argtypes, so on 64-bit Windows the GetCurrentProcess
            # pseudo-handle was marshalled as a 32-bit int and the call
            # failed silently ("SetPriorityClass(HIGH) returned 0") — we
            # never actually ran at HIGH, which is exactly the priority
            # bump meant to win scheduler contests against iRacing. psutil
            # sets the priority class correctly and reports failures.
            import psutil

            psutil.Process().nice(psutil.HIGH_PRIORITY_CLASS)
            logger.debug("Process priority class set to HIGH")
        except Exception as exc:
            logger.debug("Could not raise process priority: %s", exc)

    app = QApplication(sys.argv)
    app.setApplicationName("OpenFOV")
    app.setOrganizationName("OpenFOV Project")
    app.setWindowIcon(app_icon())
    qss = load_stylesheet()
    if qss:
        app.setStyleSheet(qss)
    app.setQuitOnLastWindowClosed(False)  # tray keeps us alive

    # Refuse to launch if another OpenFOV is already running. Two
    # simultaneous instances fight over the same camera + FT_SharedMem
    # + NPClient registry + TrackIR.exe Job Object — one of them dies
    # in mysterious ways. Lock auto-releases on process exit (even on
    # crash) so the user can't lock themselves out.
    instance_lock = SingleInstanceLock()
    if not instance_lock.acquire():
        QMessageBox.warning(
            None,  # no parent — main window doesn't exist yet
            "OpenFOV already running",
            "OpenFOV is already running.\n\n"
            "Look for the FOV icon in your system tray (right end of "
            "the taskbar). Double-click it to bring the window back, "
            "or right-click for Show / Quit.",
        )
        return 0

    config = load_app_config()
    if camera_index is not None:
        config.camera_index = camera_index

    # First-run wizard: show before the main window if the user hasn't
    # been through it yet. We hold onto the wizard's captured neutral
    # pose so we can hand it to the pipeline once it's running.
    initial_neutral = None
    if config.show_wizard_on_next_launch:
        wiz = SetupWizard(config)
        if wiz.exec() == wiz.DialogCode.Accepted:
            config.camera_index = wiz.chosen_camera_index
            initial_neutral = wiz.neutral_pose
            # Apply the wizard's game choice as the active profile's game_id.
            chosen_profile = load_profile(config.last_profile)
            chosen_profile.game_id = wiz.chosen_game_id
            save_profile(chosen_profile)
        config.show_wizard_on_next_launch = False
        save_app_config(config)

    profile = load_profile(config.last_profile)
    window = MainWindow(
        initial_profile=profile,
        always_on_top=config.always_on_top,
    )

    # Persist always-on-top toggles back to disk.
    def _on_always_on_top(on: bool) -> None:
        config.always_on_top = on
        save_app_config(config)

    window.request_always_on_top.connect(_on_always_on_top)

    tracker = MediaPipeTracker()
    output = OutputManager()
    pipeline = PipelineThread(
        tracker=tracker,
        output=output,
        camera_index=config.camera_index,
        camera_width=config.camera_width,
        camera_height=config.camera_height,
        inference_max_dim=config.inference_max_dim,
        cv_thread_cap=config.inference_thread_cap,
        cpu_affinity_mode=config.cpu_affinity_mode,
    )
    window.attach_pipeline(pipeline)

    # Game watcher: auto-detect iRacing, route output profile changes.
    watcher = GameWatcher(profiles=list(BUILTIN_PROFILES))
    watcher.game_changed.connect(window.on_game_changed, Qt.QueuedConnection)
    # Seed the pipeline with the profile's declared game, in case the user
    # is set up but the game isn't running yet — the writer is still happy
    # to send pose data with the right GameID; iRacing reads it when it
    # launches.
    declared = get_profile(profile.game_id)
    if declared is not None:
        pipeline.set_game_output(
            declared.output,
            requires_trackir_process=declared.requires_trackir_process,
        )
    watcher.start()

    tray = Tray(app)
    tray.show_main.connect(window.showNormal)
    tray.show_main.connect(window.raise_)
    tray.recenter.connect(pipeline.request_recenter)

    hotkey_recenter = GlobalHotkey(key=config.hotkey_recenter)
    hotkey_recenter.activated.connect(pipeline.request_recenter, Qt.QueuedConnection)
    hotkey_recenter.start()

    # Toggle inference on/off entirely. The pipeline centers the in-game
    # view on each toggle and skips MediaPipe while off.
    hotkey_toggle = GlobalHotkey(key=config.hotkey_toggle_tracking)
    hotkey_toggle.activated.connect(pipeline.toggle_tracking, Qt.QueuedConnection)
    hotkey_toggle.start()

    # ---- Menu hookups ----

    def _open_settings() -> None:
        dlg = SettingsDialog(config, parent=window)
        def _apply(new_cfg) -> None:
            # Autostart toggle — propagate to the Windows registry.
            if new_cfg.start_with_windows != config.start_with_windows:
                from openfov.runtime import autostart

                ok = autostart.set_enabled(new_cfg.start_with_windows)
                if not ok and new_cfg.start_with_windows:
                    QMessageBox.information(
                        window, "Start with Windows",
                        "Could not register autostart. This usually means "
                        "you're running from a dev checkout - autostart "
                        "only works for installed builds.",
                    )
                    new_cfg = replace(new_cfg, start_with_windows=False)
            config.start_with_windows = new_cfg.start_with_windows

            # Resolution + inference downscale require a pipeline restart
            # to take effect (we'd have to tear down the MediaPipe session
            # and re-open the camera mid-flight). Persist them and inform
            # the user; they apply on next launch.
            needs_restart = (
                new_cfg.camera_width != config.camera_width
                or new_cfg.camera_height != config.camera_height
                or new_cfg.inference_max_dim != config.inference_max_dim
                or new_cfg.inference_thread_cap != config.inference_thread_cap
                or new_cfg.cpu_affinity_mode != config.cpu_affinity_mode
            )
            config.camera_width = new_cfg.camera_width
            config.camera_height = new_cfg.camera_height
            config.performance_preset = new_cfg.performance_preset
            config.inference_max_dim = new_cfg.inference_max_dim
            config.inference_thread_cap = new_cfg.inference_thread_cap
            config.cpu_affinity_mode = new_cfg.cpu_affinity_mode

            # Live-apply hotkey changes.
            if new_cfg.hotkey_recenter != config.hotkey_recenter:
                hotkey_recenter.set_binding(new_cfg.hotkey_recenter)
                config.hotkey_recenter = new_cfg.hotkey_recenter
            if new_cfg.hotkey_toggle_tracking != config.hotkey_toggle_tracking:
                hotkey_toggle.set_binding(new_cfg.hotkey_toggle_tracking)
                config.hotkey_toggle_tracking = new_cfg.hotkey_toggle_tracking
            save_app_config(config)

            if needs_restart:
                QMessageBox.information(
                    window, "Restart required",
                    "Capture resolution and inference downscale changes "
                    "take effect the next time OpenFOV starts.",
                )
        dlg.settings_applied.connect(_apply)
        dlg.run_wizard_requested.connect(_run_wizard_from_menu)
        dlg.exec()

    def _run_wizard_from_menu() -> None:
        wiz = SetupWizard(config)
        if wiz.exec() != wiz.DialogCode.Accepted:
            return
        config.camera_index = wiz.chosen_camera_index
        # Re-applying camera switch is best-effort; the pipeline accepts.
        pipeline.set_camera_index(wiz.chosen_camera_index)
        # Update profile's game_id if changed.
        new_profile = get_profile(wiz.chosen_game_id)
        if new_profile is not None:
            pipeline.set_game_output(
                new_profile.output,
                requires_trackir_process=new_profile.requires_trackir_process,
            )
            window._profile.game_id = wiz.chosen_game_id
        # Push the freshly-calibrated neutral straight into the pipeline.
        if wiz.neutral_pose is not None:
            pipeline.set_neutral(wiz.neutral_pose)
        save_app_config(config)

    window.request_settings.connect(_open_settings)
    window.request_wizard.connect(_run_wizard_from_menu)

    def _shutdown() -> None:
        watcher.stop()
        hotkey_recenter.stop()
        hotkey_toggle.stop()
        pipeline.stop()
        pipeline.wait(2000)
        # Save current profile + config so the next launch picks up where
        # the user left off.
        try:
            save_profile(window._profile)
            config.last_profile = window._profile.name
            save_app_config(config)
        except Exception as exc:
            logger.warning("Failed to persist state on quit: %s", exc)
        instance_lock.release()
        window.allow_close()
        window.close()
        app.quit()

    tray.quit_app.connect(_shutdown)
    window.request_quit.connect(_shutdown)

    def _on_pipeline_error(msg: str) -> None:
        QMessageBox.critical(window, "Tracker error", msg)

    pipeline.error.connect(_on_pipeline_error, Qt.QueuedConnection)

    # Hand the wizard's captured neutral to the pipeline so the user
    # doesn't have to recenter again after their calibration.
    if initial_neutral is not None:
        pipeline.set_neutral(initial_neutral)

    window.show()
    tray.show()
    pipeline.start()

    return app.exec()


# ----------------------------------------------------------------------
# Headless entry
# ----------------------------------------------------------------------


def _run_headless(use_debug: bool, camera_index: int, duration: float) -> int:
    import numpy as np  # noqa: F401

    from openfov.filtering.pipeline import PerAxisFilters
    from openfov.mapping.axis_mapper import AxisMapper
    from openfov.output.freetrack import FreeTrackWriter
    from openfov.tracker.base import TrackerSettings
    from openfov.tracker.debug_tracker import DebugSineTracker

    if use_debug:
        tracker = DebugSineTracker()
        capture = None
    else:
        try:
            import cv2  # type: ignore[import-not-found]
        except ImportError:
            logger.error("OpenCV not installed; can't run real camera. Use --debug-tracker.")
            return 2
        from openfov.tracker.mediapipe_tracker import MediaPipeTracker

        # CAP_ANY first to honor encoded indices from cv2-enumerate-cameras.
        capture = None
        for backend in (cv2.CAP_ANY, cv2.CAP_MSMF, cv2.CAP_DSHOW):
            cap = cv2.VideoCapture(camera_index, backend)
            if cap.isOpened():
                capture = cap
                break
            cap.release()
        if capture is None:
            logger.error("Could not open camera index %s on any backend", camera_index)
            return 3
        capture.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
        capture.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
        tracker = MediaPipeTracker()

    tracker.start(TrackerSettings())
    filters = PerAxisFilters()
    mapper = AxisMapper()
    writer = FreeTrackWriter()
    writer.open()

    stop = False

    def _on_sigint(_signum: int, _frame: object) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, _on_sigint)

    logger.info("OpenFOV headless pipeline running. Press Ctrl-C to stop.")
    t0 = time.monotonic()
    frame_count = 0
    last_print = t0

    try:
        while not stop:
            now = time.monotonic()
            if duration > 0 and (now - t0) >= duration:
                break

            if use_debug:
                import numpy as _np

                frame = _np.zeros((720, 1280, 3), dtype=_np.uint8)
                ts_ms = int((now - t0) * 1000)
            else:
                assert capture is not None
                ok, frame = capture.read()
                if not ok:
                    continue
                import cv2 as _cv2

                frame = _cv2.flip(frame, 1)
                ts_ms = int((now - t0) * 1000)

            result = tracker.step(frame, ts_ms)
            if result.detected:
                smoothed = filters(result.pose, t=now)
                mapped = mapper(smoothed)
                writer.write(mapped)
                frame_count += 1

                if now - last_print >= 0.2:
                    logger.info(
                        "yaw=%+6.2f pitch=%+6.2f roll=%+6.2f  fps=%.1f  inference=%.1fms",
                        mapped.yaw, mapped.pitch, mapped.roll,
                        frame_count / max(now - t0, 1e-6),
                        result.inference_ms,
                    )
                    last_print = now
    finally:
        if capture is not None:
            capture.release()
        tracker.stop()
        writer.close()

    logger.info("Stopped. %d frames in %.1fs.", frame_count, time.monotonic() - t0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
