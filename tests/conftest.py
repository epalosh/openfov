"""Shared test isolation.

Several tests construct an `OutputManager`, and `OutputManager.start()`
calls `ensure_registered()` + `TrackIRShim.start()`. On Windows those are
NOT no-ops: they rewrite the real per-user NaturalPoint registry key and
spawn a real TrackIR.exe child. Running the suite on a contributor's
machine would silently repoint their head tracking at the repo checkout.

The autouse fixture below redirects both side effects into throwaway
locations for every test, so the suite is safe to run on a machine that
actually uses OpenFOV. Tests that want to assert on registry behaviour
(see test_npclient_bootstrap.py) layer their own redirection on top.
"""

from __future__ import annotations

import contextlib
import sys

import pytest

from openfov.output import npclient_bootstrap as bootstrap

_TEST_ROOT = r"Software\OpenFOV\__pytest__"
_TEST_KEY = _TEST_ROOT + r"\NATURALPOINT\NPClient Location"
_TEST_LEGACY = _TEST_ROOT + r"\NATURALPOINT"


@pytest.fixture(autouse=True)
def _isolate_machine_state(monkeypatch, tmp_path_factory):
    """Keep the suite from touching real registry / spawning real children."""
    monkeypatch.setattr(bootstrap, "REGISTRY_KEY", _TEST_KEY, raising=False)
    monkeypatch.setattr(bootstrap, "LEGACY_REGISTRY_KEY", _TEST_LEGACY, raising=False)

    # An empty bin dir means TrackIRShim.start() finds no TrackIR.exe and
    # returns after logging, instead of launching a real process.
    monkeypatch.setenv("OPENFOV_BIN_DIR", str(tmp_path_factory.mktemp("bin")))

    yield

    if sys.platform == "win32":
        import winreg

        for key in (_TEST_KEY, _TEST_LEGACY, _TEST_ROOT + r"\NATURALPOINT",
                    _TEST_ROOT, r"Software\OpenFOV"):
            with contextlib.suppress(OSError):
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key)
