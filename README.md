# OpenFOV

[![CI](https://github.com/epalosh/openfov/actions/workflows/ci.yml/badge.svg)](https://github.com/epalosh/openfov/actions/workflows/ci.yml)
[![Latest release](https://img.shields.io/github/v/release/epalosh/openfov?display_name=tag&sort=semver)](https://github.com/epalosh/openfov/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python: 3.11+](https://img.shields.io/badge/Python-3.11%2B-blue)](https://www.python.org/downloads/)
[![Platform: Windows](https://img.shields.io/badge/Platform-Windows%2010%2F11-blue)](#install)

Webcam head tracking for iRacing.

## Install

Download `OpenFOV-x.y.z-setup.exe` from
[Releases](https://github.com/epalosh/openfov/releases) and run it.
The installer also fetches the Microsoft Visual C++ Runtime if your
machine doesn't already have it.

On first launch Windows may show *"Windows protected your PC."*
Click **More info → Run anyway**. OpenFOV is currently shipped unsigned. 
Once signed, this prompt goes away for new downloads.

See [docs/INSTALL.md](docs/INSTALL.md) for the full install + setup +
uninstall walkthrough.

## Quick start

1. Run **OpenFOV**.
2. The first-run wizard walks you through: pick a webcam → calibrate
   your neutral pose (look straight, press the button) → read the
   in-game tips → done.
3. Launch iRacing. TrackIR should be enabled by default!
4. Drive.

To recenter your view at any time, press **F9**.

## Troubleshooting

**Antivirus.** Some Defender definitions flag the bundled `TrackIR.exe` as
malware (a false positive — it does nothing but sleep) and quarantine it out
of `resources\bin\`. If tracking stopped working after an AV scan, check
that folder still contains `NPClient64.dll` and `TrackIR.exe`.

## Architecture

```
Webcam → MediaPipe FaceLandmarker → One Euro filter
                                      ↓
                         per-axis Bezier curve + invert
                                      ↓
                          FT_SharedMem (FreeTrack proto)
                                      ↓
                      bundled NPClient64.dll (loaded by iRacing)
                                      ↓
                                 iRacing
```

OpenFOV writes the FreeTrack shared-memory section directly, and a bundled
`NPClient64.dll` (clean MIT source, originally from
[linux-track](https://github.com/uglyDwarf/linuxtrack)) exposes the NaturalPoint
TrackIR API to iRacing.

## Tech stack

- Python 3.12 with PySide6 (Qt 6) for the UI
- [MediaPipe FaceLandmarker](https://developers.google.com/mediapipe/solutions/vision/face_landmarker)
  for 478-landmark face tracking
- One Euro Filter for low-lag / low-jitter smoothing
- Compiled to a standalone Windows binary with [Nuitka](https://nuitka.net)
- Wrapped in [Inno Setup](https://jrsoftware.org/isinfo.php) for installation

## License

MIT. See [LICENSE](LICENSE). Third-party attributions are in [NOTICE](NOTICE).

## Trademarks

TrackIR is a trademark of NaturalPoint, Inc. OpenFOV is not affiliated with,
endorsed by, or sponsored by NaturalPoint. Mentions of TrackIR and the
NPClient API exist only to describe interoperability with third-party games
that implement support for that API.
