# Installing OpenFOV

## Direct download

1. Go to [Releases](https://github.com/epalosh/openfov/releases)
   and download `OpenFOV-x.y.z-setup.exe`.
2. Double-click to run.
3. Click **More info → Run anyway** if Windows shows the SmartScreen
   warning shown below — this is expected for unsigned open-source
   software. The warning will disappear once our certificate is issued.

![Windows SmartScreen warning](smartscreen.png)

4. The installer walks you through Program Files installation. Defaults
   are good — desktop shortcut and start-with-Windows are both opt-in.

## What gets installed

- **`C:\Program Files\OpenFOV\OpenFOV.exe`** — the main app, plus its
  bundled Python runtime + MediaPipe + OpenCV + Qt.
- **`...\OpenFOV\resources\bin\NPClient.dll` / `NPClient64.dll`** —
  the freestanding TrackIR shim. iRacing reads these when you enable
  TrackIR in its graphics options.
- **`...\OpenFOV\resources\bin\TrackIR.exe`** — a small dummy process
  (a few KB) that satisfies games that check for `TrackIR.exe` being
  alive. Auto-launched by OpenFOV while tracking is active; auto-killed
  when you quit.
- **`%APPDATA%\OpenFOV\config.toml`** and `profiles\*.toml` — your
  per-user settings. Survives uninstall by default so reinstalling
  keeps your tuning.

## Setting up iRacing

1. Launch OpenFOV. The first-run wizard walks you through:
   - Picking your webcam.
   - Calibrating your neutral pose.
   - Selecting iRacing as the default game profile.
2. Open iRacing.
3. Get in a car. Look around. Your head moves the camera.

That's it.

## Uninstalling

- **Settings → Apps → OpenFOV → Uninstall**, or
- Start menu → OpenFOV → Uninstall OpenFOV.

This removes the Program Files install. Your profiles under
`%APPDATA%\OpenFOV\` are left intact unless you wipe them manually —
makes reinstalling painless.

The `HKCU\Software\NaturalPoint\NATURALPOINT\NPClient Location`
registry key is written by the app at first run, not by the installer.
It's harmless and gets overwritten if you later install another
TrackIR-compatible app or NaturalPoint's official TrackIR.

## Troubleshooting

**"Windows protected your PC" won't go away.**
Click *More info* first, *then* click *Run anyway*. The button only
appears after expanding the info.

**No webcam in the dropdown.**
Make sure no other app (Zoom, OBS, Teams) has exclusive use of the
camera. OpenFOV uses Media Foundation by default and falls back to
DirectShow.

**Tracking works but iRacing doesn't move the view.**
There are known issues with getting OpenFOV connected to iRacing on some 
users' setups. We are working on a solution! In the meantime, see the "Issues"
tab for discussion on this, and how you might be able to fix the issue manually
until a new release is dropped. ETA before 6/14. Thank you for your patience!

**The app reports "camera disconnected".**
OpenFOV auto-retries on USB reconnect. Just plug your webcam back in;
the status bar will flip to "reconnected" within a couple of seconds.

**Where are the logs?**
`%APPDATA%\OpenFOV\openfov.log` (when the app is built with logging
enabled in a release build). For dev runs, logs go to stderr.

## What about NaturalPoint's actual TrackIR hardware?

If you own real TrackIR hardware, NaturalPoint's own NPClient.dll takes
priority — the registry key is theirs to manage too. OpenFOV's setup
detects this on launch and yields cleanly: no duplicate TrackIR.exe
processes, no fighting over `FT_SharedMem`.
