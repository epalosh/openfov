"""iRacing GameProfile.

iRacing's TrackIR integration is registered under a specific NaturalPoint
program-profile ID. The encryption key for that game-id is left empty in
v1; OpenFOV's NPClient stub detects an all-zero key and skips XOR.

If we discover that iRacing requires a non-zero key for some titles (it
historically did not for the main sim — only Falcon BMS-style games tend
to enforce this), we fill it in here. The architecture supports a per-game
key without code changes elsewhere.

Process names cover the current iRacing build (DX11) and the legacy DX9
(in case anyone is running an old install):
- iRacingSim64DX11.exe — current
- iRacingSim64.exe     — legacy"""

from __future__ import annotations

from openfov.games.base import GameProfile
from openfov.output.manager import GameOutputProfile

# Program-profile ID iRacing passes to NP_RegisterProgramProfileID.
#
# Measured, not guessed: with OpenFOV feeding FT_SharedMem, iRacing's
# NPClient call wrote GameId=14101 into the shared section (2026.06 build,
# iRacingSim64DX11.exe). Earlier releases hardcoded 1001, which was wrong.
#
# This is cosmetic today — NPClient only consults GameId to decide whether
# to pick up a per-game XOR table, and iRacing's key is all zeros — but a
# wrong ID would silently defeat encryption negotiation for any title that
# does use one, so keep it accurate.
_IRACING_PROGRAM_ID = 14101

IRACING_PROFILE = GameProfile(
    id="iracing",
    display_name="iRacing",
    process_names=(
        "iRacingSim64DX11.exe",
        "iRacingSim64.exe",
    ),
    output=GameOutputProfile(
        game_id=_IRACING_PROGRAM_ID,
        encryption_key=b"\x00" * 8,
    ),
    # No per-axis defaults overrides — the default profile is good for
    # iRacing out of the box.
    default_axes=None,
)


__all__ = ["IRACING_PROFILE"]
