# OpenFOV v0.1.0

Webcam head tracking for iRacing!

## What this is

OpenFOV turns any ordinary webcam into a head tracker for iRacing.
It watches your face with MediaPipe, smooths the result, and feeds it to games
through the same TrackIR / NPClient interface they already support.

This is the first public release. It targets iRacing specifically, and ships
everything needed to go from installer to working in-game tracking in a few
minutes.

## Highlights

- **Webcam head tracking** powered by MediaPipe's 478-point face landmarker.
  Any USB or built-in webcam works.
- **iRacing out of the box.** OpenFOV detects iRacing automatically and
  registers itself as a TrackIR device through a bundled NPClient surface, so
  the game's existing **Options → Graphics → TrackIR** toggle is all you flip.
- **First-run wizard** walks you through camera pick, neutral-pose
  calibration, and game selection. You're tracking before you've read any
  docs.
- **Per-axis tuning** for yaw, pitch, and roll: invert, sensitivity, and a
  full Bezier response curve editor (drag anchors, right-click presets,
  live indicator that follows your head).
- **Adaptive smoothing** via a One Euro filter (low lag when you move, low
  jitter when you don't), plus median-based outlier rejection and a
  configurable dead-zone around center.
- **Named profiles** save the whole tuning state — keep separate setups for
  GT3, formula cars, or different rigs, and switch from the profile bar.
- **3D pose widget** renders a small live head model so you can see exactly
  what the tracker is sending the game.
- **Configurable hotkeys** with **F9 recenter** out of the box; rebind in
  Settings without restarting.
- **Performance presets** (Performance / Balanced / Quality) tune resolution
  and inference downscale to match your CPU.
- **System tray** integration: Show / Recenter / Pause / Quit. Close hides
  to tray; OpenFOV keeps running in the background.

## Install

**Recommended (no SmartScreen warning, available shortly after release):**

```pwsh
winget install OpenFOV
```

**Direct download:** grab `OpenFOV-0.1.0-setup.exe` from the
[Releases page](https://github.com/epalosh/openfov/releases).

On first launch Windows may show *"Windows protected your PC."* Click
**More info → Run anyway**. The build is currently unsigned while our
SignPath Foundation certificate is in review; every release includes a
VirusTotal scan link for verification. Full walkthrough in
[docs/INSTALL.md](INSTALL.md).

## System requirements

- Windows 10 or 11 (64-bit)
- Any DirectShow or Media Foundation webcam
- ~4 GB RAM, modern dual-core CPU (a quad-core is comfortable)
- iRacing — or any game implementing the TrackIR / NPClient API

## Known limitations

- **iRacing is the only validated game.** The NPClient surface is generic
  and should work with other TrackIR-aware titles, but they aren't tested
  yet. Reports welcome.
- **CPU inference can dip below 60 fps** under heavy GPU load on lower-end
  CPUs. Switch to the **Performance** preset if you see stutter.
- **Unsigned binaries** trigger SmartScreen until our code-signing
  certificate clears. See the install walkthrough.
- **No multi-monitor FOV expansion** in this release — output is pose only.
- Lighting matters: tracking quality drops in very dim or strongly
  back-lit scenes.

## License & acknowledgments

OpenFOV is released under the **MIT License**. Full third-party attributions
are in [NOTICE](../NOTICE).

The bundled `NPClient.dll` / `NPClient64.dll` are built from the MIT-licensed
NPClient stub originally written by Michal Navratil (uglyDwarf) for
[linux-track](https://github.com/uglyDwarf/linuxtrack), via opentrack's
`contrib/npclient/` directory. Thanks to both projects.

Also built on MediaPipe, OpenCV, NumPy, PySide6, psutil, and pynput.

TrackIR is a trademark of NaturalPoint, Inc. OpenFOV is not affiliated with
or endorsed by NaturalPoint.
