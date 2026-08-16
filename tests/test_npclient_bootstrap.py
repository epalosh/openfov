"""NPClient bootstrap helpers.

The registry *layout* is the thing that actually matters here, and it is
what silently broke iRacing support in every release up to 0.2.1: the
directory was written as a value named "NPClient Location" on the parent
key, instead of into a value named "Path" under a subkey of that name —
and without the trailing separator games rely on. Nothing in the old test
suite looked at the layout, so it shipped three times.

These tests now pin both halves of that contract:
  * `normalize_dll_dir` is pure string work, so the trailing-separator and
    forward-slash invariants are asserted on every OS.
  * The Windows-only round-trip tests redirect the module's key constants
    to a throwaway HKCU subtree, so they never touch real NaturalPoint
    state, and assert the value lands where a game would look for it.
"""

from __future__ import annotations

import contextlib
import sys
from pathlib import Path

import pytest

from openfov.output import npclient_bootstrap as bootstrap

# ---------------------------------------------------------------------------
# bin dir resolution
# ---------------------------------------------------------------------------


def test_bundled_bin_dir_respects_env_override(
    monkeypatch, tmp_path: Path
) -> None:
    target = tmp_path / "custom_bin"
    target.mkdir()
    monkeypatch.setenv("OPENFOV_BIN_DIR", str(target))
    resolved = bootstrap.bundled_bin_dir()
    assert resolved == target.resolve()


def test_bundled_bin_dir_default_under_repo(monkeypatch) -> None:
    """In a dev checkout, the default points at resources/bin under repo root."""
    monkeypatch.delenv("OPENFOV_BIN_DIR", raising=False)
    p = bootstrap.bundled_bin_dir()
    assert p.name == "bin"
    assert p.parent.name == "resources"


# ---------------------------------------------------------------------------
# The value format games actually consume. Runs everywhere.
# ---------------------------------------------------------------------------


def test_normalize_ends_with_separator() -> None:
    """Games do `LoadLibrary(reg_value + "NPClient64.dll")` with no
    separator of their own. Without the trailing slash the game looks for
    `...\\binNPClient64.dll` and silently finds nothing."""
    assert bootstrap.normalize_dll_dir(r"C:\Program Files\OpenFOV\resources\bin").endswith("/")


def test_normalize_uses_forward_slashes() -> None:
    out = bootstrap.normalize_dll_dir(r"C:\Program Files\OpenFOV\resources\bin")
    assert "\\" not in out
    assert out == "C:/Program Files/OpenFOV/resources/bin/"


def test_normalize_is_idempotent() -> None:
    once = bootstrap.normalize_dll_dir(r"C:\OpenFOV\bin")
    assert bootstrap.normalize_dll_dir(once) == once


@pytest.mark.parametrize(
    "raw",
    [
        r"C:\OpenFOV\bin",
        "C:/OpenFOV/bin",
        "C:/OpenFOV/bin/",
        "C:/OpenFOV/bin///",
        r"C:\OpenFOV\bin\\",
    ],
)
def test_normalize_collapses_to_single_trailing_slash(raw: str) -> None:
    assert bootstrap.normalize_dll_dir(raw) == "C:/OpenFOV/bin/"


def test_normalized_value_concatenates_to_a_real_file(tmp_path: Path) -> None:
    """The end-to-end invariant, done exactly the way a game does it:
    string concatenation, no os.path.join."""
    binp = tmp_path / "resources" / "bin"
    binp.mkdir(parents=True)
    (binp / bootstrap.CLIENT_DLL_NAME).write_bytes(b"MZ")
    value = bootstrap.normalize_dll_dir(binp)
    assert Path(value + bootstrap.CLIENT_DLL_NAME).exists()


def test_unnormalized_value_would_not_resolve(tmp_path: Path) -> None:
    """Regression guard for the actual 0.2.1 bug — proves the naive form
    genuinely fails, so the test above isn't vacuous."""
    binp = tmp_path / "resources" / "bin"
    binp.mkdir(parents=True)
    (binp / bootstrap.CLIENT_DLL_NAME).write_bytes(b"MZ")
    assert not Path(str(binp) + bootstrap.CLIENT_DLL_NAME).exists()


# ---------------------------------------------------------------------------
# Registry layout. Windows only; writes into a redirected test subtree.
# ---------------------------------------------------------------------------

pytestmark_win = pytest.mark.skipif(
    sys.platform != "win32", reason="registry round-trip is Windows-only"
)


@pytest.fixture()
def sandboxed_registry(monkeypatch):
    """Point the module at a throwaway HKCU subtree and clean up after."""
    import winreg

    key = r"Software\OpenFOV\__test__\NATURALPOINT\NPClient Location"
    legacy = r"Software\OpenFOV\__test__\NATURALPOINT"
    monkeypatch.setattr(bootstrap, "REGISTRY_KEY", key)
    monkeypatch.setattr(bootstrap, "LEGACY_REGISTRY_KEY", legacy)
    yield key, legacy
    for k in (key, legacy, r"Software\OpenFOV\__test__", r"Software\OpenFOV"):
        with contextlib.suppress(OSError):
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, k)


@pytestmark_win
def test_write_lands_in_subkey_under_path_value(sandboxed_registry, tmp_path: Path) -> None:
    """The exact lookup a game performs: open the `NPClient Location`
    SUBKEY, read the value named `Path`."""
    import winreg

    key, _ = sandboxed_registry
    bootstrap.write_registry_path(tmp_path)

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_READ) as k:
        value, kind = winreg.QueryValueEx(k, "Path")
    assert kind == winreg.REG_SZ
    assert value.endswith("/")
    assert "\\" not in value


@pytestmark_win
def test_write_does_not_use_parent_value(sandboxed_registry, tmp_path: Path) -> None:
    """Guards against regressing to the 0.2.1 layout."""
    import winreg

    _, legacy = sandboxed_registry
    bootstrap.write_registry_path(tmp_path)

    with (
        winreg.OpenKey(winreg.HKEY_CURRENT_USER, legacy, 0, winreg.KEY_READ) as k,
        pytest.raises(FileNotFoundError),
    ):
        winreg.QueryValueEx(k, "NPClient Location")


@pytestmark_win
def test_round_trip_read_matches_write(sandboxed_registry, tmp_path: Path) -> None:
    written = bootstrap.write_registry_path(tmp_path)
    assert bootstrap.read_registry_path() == written


@pytestmark_win
def test_verify_registration_detects_missing_dll(sandboxed_registry, tmp_path: Path) -> None:
    bootstrap.write_registry_path(tmp_path)
    assert bootstrap.verify_registration() is False

    (tmp_path / bootstrap.CLIENT_DLL_NAME).write_bytes(b"MZ")
    assert bootstrap.verify_registration() is True


@pytestmark_win
def test_purge_legacy_value_removes_stray(sandboxed_registry, tmp_path: Path) -> None:
    import winreg

    _, legacy = sandboxed_registry
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, legacy) as k:
        winreg.SetValueEx(k, "NPClient Location", 0, winreg.REG_SZ, str(tmp_path))

    assert bootstrap.purge_legacy_value() is True
    assert bootstrap.purge_legacy_value() is False


@pytestmark_win
def test_remove_registry_path_clears_value(sandboxed_registry, tmp_path: Path) -> None:
    bootstrap.write_registry_path(tmp_path)
    assert bootstrap.remove_registry_path() is True
    assert bootstrap.read_registry_path() is None


# ---------------------------------------------------------------------------
# Cross-platform safety
# ---------------------------------------------------------------------------


def test_read_registry_no_op_off_windows() -> None:
    if sys.platform != "win32":
        assert bootstrap.read_registry_path() is None


def test_remove_registry_no_op_off_windows() -> None:
    if sys.platform != "win32":
        assert bootstrap.remove_registry_path() is False


def test_write_registry_no_op_off_windows() -> None:
    """On non-Windows the write should silently no-op (logged, not raised)
    but still return the normalized value."""
    if sys.platform != "win32":
        assert bootstrap.write_registry_path("/tmp/nowhere") == "/tmp/nowhere/"
