# OpenFOV

Webcam head tracking for iRacing.
One installer, one launch — head tracking working.

## Install

Download `OpenFOV-x.y.z-setup.exe` from
[Releases](https://github.com/epalosh/openfov/releases) and run it.
The installer also fetches the Microsoft Visual C++ Runtime if your
machine doesn't already have it.

On first launch Windows may show *"Windows protected your PC."*
Click **More info → Run anyway**. OpenFOV is currently shipped unsigned
while a free SignPath Foundation certificate is being approved; once
signed, this prompt goes away for new downloads.

A WinGet manifest will follow shortly after the first release lands —
once merged, `winget install epalosh.OpenFOV` will work too.

See [docs/INSTALL.md](docs/INSTALL.md) for the full install + setup +
uninstall walkthrough, including a SmartScreen screenshot.

## Quick start

1. Run **OpenFOV**.
2. The first-run wizard walks you through: pick a webcam → calibrate
   your neutral pose (look straight, press the button) → read the
   in-game tips → done.
3. Launch iRacing. In **Options → Graphics**, enable **TrackIR**.
4. Drive.

To recenter your view at any time, press **F9**.

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
