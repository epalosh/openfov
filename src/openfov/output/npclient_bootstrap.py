"""NPClient registry bootstrap.

Games locate NPClient.dll through the NaturalPoint registry key

    HKEY_CURRENT_USER\\Software\\NaturalPoint\\NATURALPOINT\\NPClient Location

`NPClient Location` is a **subkey**, and the directory lives in a value named
**`Path`** inside it. Getting either half wrong makes the lookup fail
silently: the game finds nothing, never calls `LoadLibrary`, and reports no
error. (iRacing's own binary contains this exact key path immediately
followed by the string `NPClient64.dll`.)

The stored value must also end in a separator. Games concatenate the DLL
filename straight onto it::

    LoadLibrary(reg_value + "NPClient64.dll")

so `...\\resources\\bin` resolves to `...\\resources\\binNPClient64.dll` and
silently finds nothing. opentrack normalizes to forward slashes and appends
`/`; we emit the identical form, since that is what every TrackIR-aware
title is known to accept.

Per-user (HKCU) — never HKLM — so no UAC, and the install is fully scoped
to whoever runs OpenFOV.

On non-Windows the module exposes the same API but every function becomes
a no-op + log message, so CI on Linux/macOS can import freely.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

# Canonical NaturalPoint location: a subkey, with the directory in `Path`.
REGISTRY_KEY = r"Software\NaturalPoint\NATURALPOINT\NPClient Location"
REGISTRY_VALUE = "Path"

# OpenFOV <= 0.2.1 wrote the directory as a *value* named "NPClient Location"
# on the parent key instead. No game ever read that, so tracking silently
# did nothing. We delete the stray value on every launch so upgrading users
# don't keep a confusing orphan in their registry forever.
LEGACY_REGISTRY_KEY = r"Software\NaturalPoint\NATURALPOINT"
LEGACY_REGISTRY_VALUE = "NPClient Location"

# What games append to the registered directory. Used to verify our own
# write actually resolves to a real file.
CLIENT_DLL_NAME = "NPClient64.dll"


def normalize_dll_dir(path: Path | str) -> str:
    """Render `path` the way TrackIR-aware games expect to consume it.

    Forward slashes, guaranteed single trailing slash. Pure string work so
    it is testable on any OS — this is the exact invariant whose absence
    broke iRacing support in 0.1.0 through 0.2.1.
    """
    text = str(path).replace("\\", "/")
    return text.rstrip("/") + "/"


def bundled_bin_dir() -> Path:
    """Resolve the directory that contains NPClient.dll / NPClient64.dll /
    TrackIR.exe at runtime.

    - In a Nuitka standalone build, the binaries live alongside our exe.
    - In a development checkout, they live under `resources/bin/`.
    - The `OPENFOV_BIN_DIR` env var overrides everything (useful for tests).
    """
    import os

    override = os.environ.get("OPENFOV_BIN_DIR")
    if override:
        return Path(override).resolve()

    # Walk up from this file to find the project root, then check the dev
    # path; if the bundled-resources path next to the exe exists, prefer
    # that.
    # Detect a packaged build. PyInstaller sets sys.frozen / _MEIPASS;
    # Nuitka sets neither but attaches `__compiled__` to every compiled
    # module. Without the `__compiled__` check we'd fall through to the dev
    # path below, whose parents[3] over-shoots to the dist/ root inside a
    # Nuitka bundle — the cause of the "NPClient binaries not found"
    # warning. (asset_path in ui/resources.py already detects Nuitka this
    # way; this keeps bin resolution consistent with it.)
    if hasattr(sys, "frozen") or getattr(sys, "_MEIPASS", None) or "__compiled__" in globals():
        exe_dir = Path(sys.argv[0]).resolve().parent
        candidate = exe_dir / "resources" / "bin"
        if candidate.exists():
            return candidate
        return exe_dir / "bin"

    repo_root = Path(__file__).resolve().parents[3]
    return repo_root / "resources" / "bin"


# ---------------------------------------------------------------------------
# Win32 plumbing (registry I/O via winreg). winreg is part of the stdlib on
# Windows; we wrap each call so non-Windows OSes can import this module
# without ImportError.
# ---------------------------------------------------------------------------


def _is_windows() -> bool:
    return sys.platform == "win32"


def read_registry_path() -> str | None:
    """Return the currently-registered NPClient location, or None if unset.

    Reads the canonical subkey — i.e. exactly what a game reads, so this is
    a truthful health check rather than an echo of our own write.
    """
    if not _is_windows():
        return None
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REGISTRY_KEY, 0, winreg.KEY_READ) as k:
            value, _ = winreg.QueryValueEx(k, REGISTRY_VALUE)
            return str(value) if value else None
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.warning("Could not read NPClient registry key: %s", exc)
        return None


def write_registry_path(path: Path | str) -> str:
    """Point the registry at the given directory. Creates the key tree if
    missing. Idempotent — safe to call on every app launch.

    Returns the string actually written (normalized), so callers can log or
    verify it.
    """
    if not _is_windows():
        logger.debug("write_registry_path no-op on non-Windows")
        return normalize_dll_dir(path)
    import winreg

    value = normalize_dll_dir(Path(path).resolve())
    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, REGISTRY_KEY) as k:
            winreg.SetValueEx(k, REGISTRY_VALUE, 0, winreg.REG_SZ, value)
        logger.info(r"NPClient registry set: HKCU\%s\%s = %s", REGISTRY_KEY, REGISTRY_VALUE, value)
    except OSError as exc:
        logger.error("Failed to write NPClient registry key: %s", exc)
        raise
    return value


def purge_legacy_value() -> bool:
    """Delete the pre-0.2.2 stray value on the parent key. Returns True if
    one was actually removed."""
    if not _is_windows():
        return False
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, LEGACY_REGISTRY_KEY, 0, winreg.KEY_SET_VALUE
        ) as k:
            winreg.DeleteValue(k, LEGACY_REGISTRY_VALUE)
        logger.info("Removed legacy NPClient registry value left by an older OpenFOV")
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        logger.debug("Could not remove legacy NPClient value: %s", exc)
        return False


def remove_registry_path() -> bool:
    """Delete our `Path` value (and any legacy stray), leaving other
    NaturalPoint state intact. Returns True if anything was removed.
    Called by the uninstaller."""
    if not _is_windows():
        return False
    import winreg

    removed = False
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, REGISTRY_KEY, 0, winreg.KEY_SET_VALUE
        ) as k:
            winreg.DeleteValue(k, REGISTRY_VALUE)
        logger.info("NPClient registry value removed")
        removed = True
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.warning("Could not remove NPClient registry value: %s", exc)

    return purge_legacy_value() or removed


# ---------------------------------------------------------------------------
# High-level orchestration
# ---------------------------------------------------------------------------


def verify_registration() -> bool:
    """Read back what a game would read and confirm it resolves to a real
    NPClient64.dll.

    This is the check whose absence let a broken registry layout ship in
    every release up to 0.2.1: the app wrote a value, read back its own
    value, and reported success — while no game could find anything.
    """
    if not _is_windows():
        return False
    value = read_registry_path()
    if not value:
        logger.error(
            r"NPClient registration MISSING: HKCU\%s\%s is unset. "
            "Games cannot find OpenFOV; head tracking will not work.",
            REGISTRY_KEY,
            REGISTRY_VALUE,
        )
        return False
    # Reproduce the game's own concatenation exactly — no os.path.join,
    # because the game doesn't use one either.
    resolved = Path(value + CLIENT_DLL_NAME)
    if not resolved.exists():
        logger.error(
            "NPClient registration BROKEN: registry says %r, but %s does not "
            "exist. Games will silently fail to load head tracking.",
            value,
            resolved,
        )
        return False
    logger.info("NPClient registration verified: %s", resolved)
    return True


def ensure_registered() -> Path:
    """Ensure the NPClient registry key points at our bundled `bin/` dir.

    Returns the path that's now registered. Logs an error (but does not
    raise) if the resulting registration doesn't resolve to a real DLL —
    the app stays usable for camera/preview work either way.
    """
    bin_dir = bundled_bin_dir()
    npclient = bin_dir / "NPClient.dll"
    npclient64 = bin_dir / CLIENT_DLL_NAME

    # In development we may be running before build.ps1 has produced the
    # DLLs. Warn but don't fail — useful for UI dev where the game isn't in
    # play anyway. The user-facing message stays generic; the dev-only
    # build instruction is logged at DEBUG.
    if not npclient.exists() and not npclient64.exists() and _is_windows():
        logger.warning(
            "NPClient binaries not found in %s — OpenFOV install may be "
            "incomplete, or your antivirus may have quarantined them. "
            "Please reinstall.",
            bin_dir,
        )
        logger.debug(
            "Dev-mode hint: run npclient-vendor/build.ps1 to populate "
            "resources/bin/."
        )
    purge_legacy_value()
    write_registry_path(bin_dir)
    verify_registration()
    return bin_dir


__all__ = [
    "CLIENT_DLL_NAME",
    "LEGACY_REGISTRY_KEY",
    "LEGACY_REGISTRY_VALUE",
    "REGISTRY_KEY",
    "REGISTRY_VALUE",
    "bundled_bin_dir",
    "ensure_registered",
    "normalize_dll_dir",
    "purge_legacy_value",
    "read_registry_path",
    "remove_registry_path",
    "verify_registration",
    "write_registry_path",
]
