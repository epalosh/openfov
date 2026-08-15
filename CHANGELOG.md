# Changelog

All notable changes to OpenFOV are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.2.2] — iRacing head tracking actually works

### Fixed
- **Games could never find OpenFOV's NPClient DLL.** This is the root cause
  behind every "OpenFOV doesn't do anything in iRacing" report, and it has
  been present since the very first release. Two defects in the same
  registry write, both required for a game to find us
  ([#12](https://github.com/epalosh/openfov/issues/12)):
  - The directory was written as a **value** named `NPClient Location` on
    `HKCU\Software\NaturalPoint\NATURALPOINT`. Games open
    `...\NATURALPOINT\NPClient Location` as a **subkey** and read a value
    named **`Path`** inside it. That subkey never existed, so the lookup
    failed and no game ever called `LoadLibrary`.
  - The value had no trailing separator. Games concatenate the DLL name
    directly onto it (`reg_value + "NPClient64.dll"`), so even a correctly
    placed value resolved to `...\resources\binNPClient64.dll`. We now emit
    the same form opentrack does: forward slashes, guaranteed trailing `/`.

  Both failures were silent — no error, no log line, no in-game message.
  Upgrading is enough; the stale value from older installs is removed
  automatically on next launch.
- iRacing's program-profile ID corrected to **14101** (was a guessed 1001),
  measured from what the sim writes into `FT_SharedMem`.

### Added
- `verify_registration()` — after registering, OpenFOV now reads the key
  back *the way a game reads it* and confirms the result resolves to a real
  `NPClient64.dll`, logging a loud error if not. Previously the app wrote a
  value, read back its own value, and reported success while no game could
  find anything.
- Regression tests pinning the registry layout: the trailing-separator and
  forward-slash invariants (all platforms), plus Windows round-trip tests
  asserting the value lands in the subkey a game reads and *not* on the
  parent key. The old suite deferred this to "an integration test on
  Windows CI" that never existed, which is why the bug shipped three times.

### Fixed — antivirus false positives
- **Microsoft Defender classified the bundled `TrackIR.exe` as
  `Trojan:Win32/Ravartar!rfn` (Severe) and quarantined it** out of
  `resources\bin\`, which silently broke head tracking for the games that
  need it. Three separate things made it look like malware, all now
  addressed:
  - Its whole body was `for(;;) Sleep(INFINITE);`. A tiny, stripped,
    unsigned executable whose entry point sleeps forever is the classic
    sandbox-evasion stub. It is now an ordinary Win32 program with a
    message-only window and a real message pump. Still 0% CPU at idle.
  - It shipped with no version resource and `-Wl,--strip-all`. It now
    carries honest version metadata (identifying OpenFOV, not NaturalPoint)
    and is no longer stripped.
  - OpenFOV launched it `CREATE_SUSPENDED`, walked a thread snapshot, and
    called `ResumeThread` — the textbook process-injection fingerprint.
    Replaced with `STARTUPINFOEX` + `PROC_THREAD_ATTRIBUTE_JOB_LIST`, which
    gives the same no-orphans guarantee atomically at process creation.

  On the machine that reproducibly quarantined the old build, Defender now
  reports no threats on the rebuilt one. That is one machine and one
  definition set, not a guarantee.
- The helper is no longer launched at all unless the active game needs it.
  Most titles — iRacing included — find OpenFOV purely through the NPClient
  registry key and never look at the process list. See
  `GameProfile.requires_trackir_process` (default off).

### Added — you can now tell whether it's actually working
- The main window distinguishes **"a game is running"** from **"a game is
  reading your head tracking"**. Those are different claims, and conflating
  them is why a completely dead output path looked healthy for three
  releases. The new indicator is real end-to-end proof: NPClient's
  `NP_RegisterProgramProfileID` writes `GameId` and never `GameId2`, while
  OpenFOV always writes both, so an inequality can only have come from a
  game that loaded our DLL and called in.
- `tools/verify_install.py` runs a real game's discovery sequence against an
  install and exits non-zero, with a specific reason, if a game could not
  use it. CI and the release workflow both run it — the release build
  installs the actual installer first — plus a negative control that fails
  the build if the check ever stops detecting known breakage.

### Fixed — first-run friction
- The camera picker listed a single webcam twice (`1400: USB Video Device`
  and `700: USB Video Device` — the same device via two capture backends,
  labelled with an internal index). Now one entry per physical device, by
  friendly name.
- The wizard's calibrate page was a dead end when no face was detected:
  Calibrate disabled → Next disabled → Cancel the only way out. Added
  **Skip for now**.
- Cancelling the wizard no longer discards the camera you just picked.

### Known issues
- The installer is still **unsigned**, so Windows will show *"Unknown
  publisher"* / SmartScreen warnings until code signing is in place.

## [0.2.1] — Head-tracking feel + game-contention performance

> First build actually published by CI. v0.2.0 was tagged but never
> shipped: the release workflow used `secrets` in a job `if:` condition,
> which GitHub rejects, so the file never parsed and no installer was ever
> built. Fixed in this release.

### Added
- Global hotkey (default **F10**) to toggle inference on/off entirely.
  Disabling stops MediaPipe (frees CPU) and snaps the in-game view to
  center; re-enabling resumes from the **last calibration** (no recenter).
  Rebindable in Settings → Hotkeys.
- **Pause preview** button — stops the camera-preview rendering (a
  diagnostic to measure its CPU cost) while tracking and the fps/inference
  readout keep running.
- CPU-contention controls so tracking holds up while a game saturates the
  CPU:
  - Window-visibility gating — preview frames + pose updates are skipped
    while the window is minimized / hidden to tray (tracking + game output
    continue).
  - OpenCV thread-pool cap (default 2) on the per-frame cvtColor / resize /
    decode work.
  - Optional **Reserve CPU cores** mode (Settings → Performance) pinning the
    process to the top logical CPUs so MediaPipe stays off the game's
    cores. Off by default; experimental.

### Changed
- Default yaw mapping is now the **soft-center curve at 3x sensitivity**
  (fine control near forward view, fast swing to the apex). Pitch and roll
  keep the gentle linear default.
- Sensitivity sliders now range **0–5x** (was 0–3x).
- Settings → Performance simplified — the raw "inference downscale" spinbox
  is gone (driven by the preset), and the OpenCV thread cap is not
  surfaced.
- Camera preview shows an explicit **"tracking disabled"** banner when
  inference is toggled off, instead of the misleading "no face detected".
- Reworded a setup-wizard tip ("Constant motion can be disorienting!").

### Fixed
- Process priority was never actually raised to HIGH on 64-bit Windows —
  the `ctypes` `SetPriorityClass` call mis-marshalled the process handle
  and failed silently. Now set via `psutil`, so OpenFOV runs at HIGH and
  stops losing scheduler contests with the game's render thread.
- Shutdown crash (`'Event' object is not callable`): the camera-reader
  thread's stop Event shadowed `threading.Thread._stop`, so every exit
  threw and skipped pipeline cleanup. Renamed the attribute.

### Packaging
- Bundle MediaPipe's `libmediapipe.dll` (~27 MB). It's loaded via `dlopen`
  at runtime, so Nuitka never saw it and `--include-package-data` skips
  DLLs — the standalone imported MediaPipe fine but the tracker failed to
  initialize ("Could not find module libmediapipe.dll").
- Bundle `NPClient64.dll` + `TrackIR.exe`. Nuitka's `--include-data-dir`
  silently skips `.dll`/`.exe`, so the installed app couldn't deliver
  tracking to iRacing (the registry pointed at a missing DLL).
- `bundled_bin_dir()` now detects a Nuitka build (`__compiled__`) so it
  resolves `<exe>/resources/bin` instead of over-shooting to `dist/`.
- Installer output renamed to `OpenFOV-<ver>-setup.exe` to match the
  release workflow, the winget manifest, and the docs.

### Tests
- **Full suite: 158/158 passing.**

## [0.1.0] — First public release

### Added
- Initial project scaffold: pyproject.toml, MIT LICENSE, NOTICE, CI skeleton.
- Phase 1 core data pipeline: tracker abstraction, MediaPipe implementation,
  One Euro filter, per-axis Bezier curve mapping, FreeTrack shared-memory
  writer, TOML-based config and profile persistence.
- Unit tests for deterministic modules (filter, curve, axis mapper,
  FreeTrack struct size).
- Headless smoke-test entry point (`python -m openfov --headless`) that runs
  the full capture → track → filter → curve → FT_SharedMem pipeline.
- Phase 2 freestanding output stack:
  - Vendored NPClient stub source (MIT, originally from linuxtrack via
    opentrack `contrib/npclient`) with proper attribution.
  - TrackIR.exe dummy source (sleeps forever; satisfies legacy process
    checks some games perform).
  - MinGW-w64 PowerShell build script producing NPClient.dll (32-bit),
    NPClient64.dll (64-bit), and TrackIR.exe.
  - `output/npclient_bootstrap.py` — manages the
    `HKCU\Software\NaturalPoint\NATURALPOINT\NPClient Location` registry
    key (set / read / remove). Per-user, no UAC.
  - `output/trackir_shim.py` — launches and terminates the bundled
    TrackIR.exe child; yields to NaturalPoint's real TrackIR if running.
  - `output/manager.py` — `OutputManager` orchestrates the FreeTrack
    writer + NPClient registration + TrackIR shim lifecycle.
  - `games/` module: `GameProfile`, `GameDetector` (psutil-based 1Hz
    polling), iRacing profile, and the builtin profile registry.
- Phase 2 native build verified: `NPClient64.dll` (18 KB) and `TrackIR.exe`
  (19 KB) built locally via MinGW-w64. DLL exports all 21 NPCLIENT.x
  ordinals at the right indices, names un-decorated.
- Phase 2 end-to-end iRacing simulation verified: loaded `NPClient64.dll`
  in-process, called `NP_QueryVersion()` → 0x0500, `NP_RegisterProgramProfileID(1001)`,
  `NP_GetData()` and confirmed yaw/pitch/roll values match exactly what
  the writer pushed (after the documented sign + radian + axis-unit
  conversions). The freestanding output chain works end-to-end.
- `TrackIRShim`: child process placed in a Windows Job Object with
  `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`, guaranteeing the bundled
  `TrackIR.exe` dies if OpenFOV crashes (no orphans). Verified by a
  subprocess crash test in the suite.
- Phase 3 UI:
  - `runtime/camera.py` — `CameraSource` wrapper preferring Media
    Foundation over DirectShow on Windows, with `enumerate_cameras()`
    providing friendly device names via `cv2-enumerate-cameras`.
  - `runtime/hotkey.py` — `GlobalHotkey` Qt-signal wrapper around pynput's
    `GlobalHotKeys` listener.
  - `runtime/pipeline.py` — `PipelineThread` (QThread) owning the full
    capture → tracker → filter → mapper → writer loop. Thread-safe
    settings updates. Per-frame `frame_ready` and `pose_ready` Qt
    signals for UI consumption.
  - `ui/camera_view.py` — `CameraView` widget. BGR frame + Nx2 landmark
    overlay drawn via QPainter, aspect-preserving scaling, "no face"
    banner.
  - `ui/pose_readout.py` — monospace yaw/pitch/roll + fps/inference stats.
  - `ui/axis_panel.py` — per-axis invert + sensitivity controls with a
    small linear-curve preview (Phase 4 makes it a full Bezier editor).
  - `ui/filter_panel.py` — per-axis One Euro `min_cutoff` + `beta` sliders.
  - `ui/profile_bar.py` — profile dropdown with Save / Save As / Rename /
    Delete buttons, persists to `%APPDATA%\OpenFOV\profiles\`.
  - `ui/tray.py` — system tray icon with Show / Recenter / Pause / Quit;
    runtime-generated placeholder icon.
  - `ui/main_window.py` — composes everything, hides-to-tray on close,
    pushes settings to the pipeline thread via signal/method calls.
  - `__main__.py` — GUI is now the default mode; `--headless` and
    `--debug-tracker` remain available for CI / offline use.
- 7 new UI smoke tests in `test_ui_smoke.py`, running under
  `QT_QPA_PLATFORM=offscreen`. Full suite: 68/68 passing.
- Phase 3 polish + Phase 4:
  - `resources/icons/openfov.ico` — real multi-resolution app icon
    (16/24/32/48/64/128/256), generated by `tools/generate_icon.py`.
    Used by the window, tray, and (later) the Inno Setup installer.
  - `resources/ui/openfov.qss` — central dark theme applied at
    `QApplication` startup; widgets no longer carry duplicate inline
    styling.
  - `ui/resources.py` — `app_icon()` + `load_stylesheet()` + `asset_path()`
    with env-var override for tests.
  - Camera USB hot-plug resilience: pipeline detects repeated read
    failures, closes the device, retries on an exponential backoff up
    to 5s, emits `camera_status(connected, message)` for the UI. Status
    bar surfaces "disconnected - retrying" / "reconnected".
  - `ui/curve_editor.py` — interactive Bezier curve editor. Draggable
    anchors (2-6), endpoints X-pinned, interior X-clamped between
    neighbors. Right-click menu for presets + add/remove. Double-click
    on empty space adds a point, double-click on an interior anchor
    removes it. Live indicator dot tracks current `(input, output)`.
  - `ui/pose_widget.py` — custom QPainter 3D rasterizer rendering a
    stylized low-poly head (subdivision-1 icosphere + small nose cone +
    darker "eye" zones), with backface culling, painter's-algorithm
    depth sort, Lambertian flat shading, and an RGB axes overlay.
  - `mapping/presets.py` — soft_center strengthened (slope 0.2 at
    center, 2.5 at edges) so users feel a real difference vs linear.
  - Integration: axis panels embed the curve editor; main window
    receives `pose_ready` from the pipeline and routes both the
    smoothed pose to the 3D widget and the live (input, output) pair
    to each axis's curve editor.
- 14 new tests (CurveEditor + PoseWidget). **Full suite: 82/82 passing.**
- Verified live on Windows: custom 4-anchor curves round-trip through
  TOML persistence bit-exactly; 3D widget renders correctly at zero
  pose and under extreme rotations.
- Phase 5 polish:
  - `runtime/game_watcher.py` — `GameWatcher` Qt-signal bridge around
    `games.GameDetector`. Emits `game_changed(GameProfile | None)` on
    the polling thread; UI consumes via `QueuedConnection`.
  - Main window now shows a "● iRacing detected" badge that flips green
    when the detector finds a matching process; profile changes
    auto-route to the pipeline as `set_game_output()` so GameID +
    encryption key update without restarting.
  - `ui/wizard.py` — first-run setup wizard. 5 pages (Welcome, Camera,
    Calibrate, Game, Finish). Lightweight live preview with optional
    MediaPipe tracker (lazy-initialized — wizard construction now
    completes in 4ms). Captures neutral pose. Outputs camera index +
    game id + neutral pose for the caller to persist.
  - `ui/hotkey_widget.py` — `HotkeyButton` captures keypresses while
    focused; translates Qt key + modifiers to pynput-compatible spec
    (`<ctrl>+<shift>+r`). Escape cancels.
  - `ui/settings_dialog.py` — tabbed settings (General / Camera /
    Hotkeys / Output). Hotkey changes live-apply through the running
    GlobalHotkey listener — no restart required to rebind F9.
  - Main window menu bar: File (Settings, Quit), Profile (Save / Save As
    / Rename / Delete), View (Always-on-top), Help (Run wizard, About).
    Ctrl+S / Ctrl+Q / Ctrl+, shortcuts.
  - `__main__.py` wired: first-launch wizard, persistent
    `show_wizard_on_next_launch` flag, game watcher started alongside
    the pipeline, settings dialog reachable from menu, optional pause
    hotkey supported.
- 14 new tests (HotkeyButton, settings dialog, game watcher Qt bridge,
  main window menu/signals). **Full suite: 96/96 passing.**
- Phase 5 limitation fixes:
  - `PipelineThread.set_neutral(pose)` — the wizard's captured neutral
    is now applied to the running pipeline before tracking begins. Same
    happens when re-running the wizard from the menu.
  - `runtime/autostart.py` — real registry implementation for "Start
    with Windows." Writes `HKCU\...\Run` only when a frozen-bundle exe
    path is resolvable, otherwise refuses gracefully (dev runs don't
    leak garbage paths into the registry). Hooked into the settings
    dialog Apply path.
  - `AppConfig.always_on_top` — persisted in `config.toml`, applied at
    main window startup, saved on every toggle.
- Phase 6 release pipeline:
  - `build/nuitka_build.ps1` — builds the standalone Nuitka distribution
    with full Windows metadata + icon + bundled resources, renames the
    output to `dist/openfov.dist/` for Inno Setup to pick up.
  - `installer/openfov.iss` — Inno Setup 6 script. Per-machine Program
    Files install, optional desktop / start-with-Windows tasks, LZMA2
    ultra compression. Uses our real icon. Leaves `%APPDATA%\OpenFOV\`
    on uninstall so reinstalls preserve profiles.
  - `.github/workflows/release.yml` — full CI release pipeline. On
    `v*.*.*` tag: installs MinGW-w64 via choco, builds native binaries,
    runs Nuitka, packages with Inno Setup, scans with VirusTotal, drafts
    a GitHub Release. SignPath signing is wired behind a
    `SIGNPATH_ENABLED` repo variable — flip it on once the Foundation
    application clears, no source changes needed.
  - `winget-pkgs-template/` — three-file manifest stub ready to PR to
    `microsoft/winget-pkgs` after the first release lands, with a
    README explaining the per-release edits.
  - `docs/INSTALL.md` — user-facing install + setup walkthrough with
    the SmartScreen workaround.
  - `docs/smartscreen.png` — labeled mockup of the SmartScreen dialog
    (generated by `tools/generate_smartscreen_mockup.py`; placeholder
    until a real install screenshot exists).
- **Full suite: 102/102 passing.**
