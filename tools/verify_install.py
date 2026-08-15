#!/usr/bin/env python3
"""End-to-end verification that a game can actually find and use OpenFOV.

This performs, step for step, the discovery sequence a TrackIR-aware title
runs — the same one iRacing's binary implements:

    1. RegOpenKey  HKCU\\Software\\NaturalPoint\\NATURALPOINT\\NPClient Location
    2. RegQueryValue "Path"                       -> a directory
    3. LoadLibrary(<Path> + "NPClient64.dll")     -> note: plain concatenation
    4. GetProcAddress + NP_GetSignature           -> must match NaturalPoint's
    5. NP_RegisterProgramProfileID / NP_GetData   -> must return live pose

Why this exists
---------------
Every release up to 0.2.1 shipped a registry pointer that no game could
read: the directory was written as a *value* named "NPClient Location" on
the parent key rather than into a value named "Path" under a subkey of
that name, and it lacked the trailing separator games concatenate onto.
Head tracking silently did nothing, and nothing in CI noticed, because CI
only ever tested Python source — never the installed artifact.

Run it against a real install:

    python tools/verify_install.py

Exit code 0 means a game would work. Non-zero means it would not, and the
message says which step broke.
"""

from __future__ import annotations

import argparse
import ctypes
import os
import sys
import time

REG_KEY = r"Software\NaturalPoint\NATURALPOINT\NPClient Location"
REG_VALUE = "Path"
CLIENT_DLL = "NPClient64.dll"

# The two 200-byte blobs NaturalPoint's API hands back, which games compare
# against a copy embedded in their own binary. iRacing carries both.
EXPECTED_DLL_SIG = (
    b"precise head tracking\n put your head into the game\n now go look "
    b"around\n\n Copyright EyeControl Technologies"
)
EXPECTED_APP_SIG = (
    b"hardware camera\n software processing data\n track user movement\n\n "
    b"Copyright EyeControl Technologies"
)


class TirData(ctypes.Structure):
    _fields_ = [
        ("status", ctypes.c_short), ("frame", ctypes.c_short),
        ("cksum", ctypes.c_uint),
        ("roll", ctypes.c_float), ("pitch", ctypes.c_float), ("yaw", ctypes.c_float),
        ("tx", ctypes.c_float), ("ty", ctypes.c_float), ("tz", ctypes.c_float),
        ("padding", ctypes.c_float * 9),
    ]


class TirSignature(ctypes.Structure):
    _fields_ = [("DllSignature", ctypes.c_char * 200),
                ("AppSignature", ctypes.c_char * 200)]


class Failure(Exception):
    pass


def step(n: int, text: str) -> None:
    print(f"  [{n}] {text}")


def read_registry() -> str:
    if sys.platform != "win32":
        raise Failure("not Windows")
    import winreg

    step(1, rf"RegOpenKey HKCU\{REG_KEY}")
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_KEY) as k:
            step(2, f'RegQueryValue "{REG_VALUE}"')
            value, _ = winreg.QueryValueEx(k, REG_VALUE)
    except FileNotFoundError as exc:
        raise Failure(
            f"registry key or value missing ({exc}). A game looks up this "
            f"exact subkey; if it is absent the game never calls "
            f"LoadLibrary and head tracking silently does nothing."
        ) from exc
    print(f"        -> {value!r}")
    if not value.endswith(("/", "\\")):
        raise Failure(
            f"registered path {value!r} has no trailing separator. Games "
            f"concatenate the DLL name directly onto it, so this resolves "
            f"to {value + CLIENT_DLL!r} — a file that does not exist."
        )
    return value


def load_client(dll_dir: str) -> ctypes.WinDLL:
    # Concatenate exactly the way the game does. NOT os.path.join: joining
    # would paper over a missing trailing separator, which is precisely the
    # bug this script exists to catch.
    path = dll_dir + CLIENT_DLL
    step(3, f"LoadLibrary({path})")
    if not os.path.exists(path):
        raise Failure(f"{path} does not exist (antivirus quarantine? bad install?)")
    try:
        return ctypes.WinDLL(path)
    except OSError as exc:
        raise Failure(f"LoadLibrary failed: {exc}") from exc


def check_signature(dll: ctypes.WinDLL) -> None:
    step(4, "NP_GetSignature")
    sig = TirSignature()
    dll.NP_GetSignature(ctypes.byref(sig))
    if sig.DllSignature != EXPECTED_DLL_SIG:
        raise Failure("DllSignature does not match what games expect")
    if sig.AppSignature != EXPECTED_APP_SIG:
        raise Failure("AppSignature does not match what games expect")
    print("        -> both signatures match NaturalPoint's")

    version = ctypes.c_ushort()
    dll.NP_QueryVersion(ctypes.byref(version))
    print(f"        -> NP_QueryVersion = 0x{version.value:04x}")
    if version.value == 0:
        raise Failure("NP_QueryVersion returned 0")


def check_data(dll: ctypes.WinDLL, game_id: int, samples: int, require_motion: bool) -> None:
    step(5, f"NP_RegisterProgramProfileID({game_id}) + NP_GetData x{samples}")
    dll.NP_RegisterProgramProfileID(ctypes.c_ushort(game_id))
    dll.NP_RequestData(ctypes.c_ushort(1))
    dll.NP_StartDataTransmission()

    seen = []
    for _ in range(samples):
        d = TirData()
        dll.NP_GetData(ctypes.byref(d))
        seen.append((d.yaw, d.pitch, d.roll))
        print(f"        status={d.status} frame={d.frame} "
              f"yaw={d.yaw:+9.1f} pitch={d.pitch:+9.1f} roll={d.roll:+9.1f}")
        time.sleep(0.15)

    if require_motion:
        if len({s for s in seen}) <= 1:
            raise Failure(
                "pose never changed across samples — OpenFOV is not writing "
                "to FT_SharedMem (is it running and tracking a face?)"
            )
        print("        -> pose is changing; data is live")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--game-id", type=int, default=14101,
                    help="program-profile ID to register as (default: iRacing's)")
    ap.add_argument("--samples", type=int, default=5)
    ap.add_argument("--require-motion", action="store_true",
                    help="also assert the pose actually changes (needs OpenFOV "
                         "running and tracking)")
    args = ap.parse_args()

    print("=" * 70)
    print("OpenFOV install verification — emulating a TrackIR-aware game")
    print("=" * 70)
    try:
        dll_dir = read_registry()
        dll = load_client(dll_dir)
        check_signature(dll)
        check_data(dll, args.game_id, args.samples, args.require_motion)
    except Failure as exc:
        print()
        print(f"FAIL: {exc}")
        print("A game would NOT be able to use OpenFOV in this state.")
        return 1

    print()
    print("PASS: a game can locate, load, and read head tracking from OpenFOV.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
