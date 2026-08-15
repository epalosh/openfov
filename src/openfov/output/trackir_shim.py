"""TrackIR.exe presence-shim lifecycle manager.

Some games (Falcon BMS, parts of MSFS) require a process named
`TrackIR.exe` to be running before they'll initialize head tracking — even
when NPClient.dll itself loads fine. OpenFOV ships a tiny helper that holds
a message-only window; we launch it while tracking is active and stop it
when we stop.

iRacing does NOT need this (it locates us purely through the NPClient
registry key), so `GameProfile.requires_trackir_process` gates it — see
`openfov.games.base`. Keeping the shim off the default path matters
because the binary attracts antivirus false positives.

If NaturalPoint's *real* TrackIR is already running (rare for users of
OpenFOV, but possible), we yield to it and don't spawn our own duplicate.

Process lifetime
----------------
The child is placed in a Windows Job Object configured with
`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`, so even if OpenFOV crashes hard the
helper dies with it. No orphan processes.

The job is attached **at creation** via `STARTUPINFOEX` +
`PROC_THREAD_ATTRIBUTE_JOB_LIST`. Previous revisions instead spawned the
child `CREATE_SUSPENDED`, walked a `CreateToolhelp32Snapshot` thread
snapshot to find its threads, and called `OpenThread`/`ResumeThread` on
them. That works, but "create suspended, enumerate threads, resume" is the
canonical process-injection fingerprint and is heavily weighted by every
behavioural AV engine — a needless risk when the documented attribute-list
API does the same job atomically and without a race.

Shutdown is cooperative: we signal a named event the helper waits on, and
only fall back to `TerminateProcess` if it doesn't exit in time.

Cross-platform safety: on non-Windows this module becomes a no-op so
tests/CI on Linux/macOS can import freely."""

from __future__ import annotations

import ctypes
import logging
import sys
from ctypes import wintypes
from pathlib import Path

from openfov.output.npclient_bootstrap import bundled_bin_dir

logger = logging.getLogger(__name__)

DUMMY_NAME = "TrackIR.exe"

# Must match SHUTDOWN_EVENT_NAME in npclient-vendor/trackir.c.
SHUTDOWN_EVENT_NAME = "Local\\OpenFOV.TrackIRShim.Shutdown"


def _is_windows() -> bool:
    return sys.platform == "win32"


def dummy_path() -> Path:
    return bundled_bin_dir() / DUMMY_NAME


# ---------------------------------------------------------------------------
# Windows plumbing.
# ---------------------------------------------------------------------------

if sys.platform == "win32":
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _JobObjectExtendedLimitInformation = 9
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000

    _CREATE_NO_WINDOW = 0x08000000
    _EXTENDED_STARTUPINFO_PRESENT = 0x00080000
    _PROC_THREAD_ATTRIBUTE_JOB_LIST = 0x0002000D

    _EVENT_MODIFY_STATE = 0x0002
    _WAIT_OBJECT_0 = 0x0
    _WAIT_TIMEOUT = 0x102

    class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_void_p),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_uint64),
            ("WriteOperationCount", ctypes.c_uint64),
            ("OtherOperationCount", ctypes.c_uint64),
            ("ReadTransferCount", ctypes.c_uint64),
            ("WriteTransferCount", ctypes.c_uint64),
            ("OtherTransferCount", ctypes.c_uint64),
        ]

    class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    class _STARTUPINFOW(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("lpReserved", wintypes.LPWSTR),
            ("lpDesktop", wintypes.LPWSTR),
            ("lpTitle", wintypes.LPWSTR),
            ("dwX", wintypes.DWORD),
            ("dwY", wintypes.DWORD),
            ("dwXSize", wintypes.DWORD),
            ("dwYSize", wintypes.DWORD),
            ("dwXCountChars", wintypes.DWORD),
            ("dwYCountChars", wintypes.DWORD),
            ("dwFillAttribute", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("wShowWindow", wintypes.WORD),
            ("cbReserved2", wintypes.WORD),
            ("lpReserved2", ctypes.c_void_p),
            ("hStdInput", wintypes.HANDLE),
            ("hStdOutput", wintypes.HANDLE),
            ("hStdError", wintypes.HANDLE),
        ]

    class _STARTUPINFOEXW(ctypes.Structure):
        _fields_ = [
            ("StartupInfo", _STARTUPINFOW),
            ("lpAttributeList", ctypes.c_void_p),
        ]

    class _PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("hProcess", wintypes.HANDLE),
            ("hThread", wintypes.HANDLE),
            ("dwProcessId", wintypes.DWORD),
            ("dwThreadId", wintypes.DWORD),
        ]

    _kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
    ]
    _kernel32.SetInformationJobObject.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.InitializeProcThreadAttributeList.argtypes = [
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.c_size_t),
    ]
    _kernel32.InitializeProcThreadAttributeList.restype = wintypes.BOOL
    _kernel32.UpdateProcThreadAttribute.argtypes = [
        ctypes.c_void_p, wintypes.DWORD, ctypes.c_size_t, ctypes.c_void_p,
        ctypes.c_size_t, ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t),
    ]
    _kernel32.UpdateProcThreadAttribute.restype = wintypes.BOOL
    _kernel32.DeleteProcThreadAttributeList.argtypes = [ctypes.c_void_p]
    _kernel32.DeleteProcThreadAttributeList.restype = None
    _kernel32.CreateProcessW.argtypes = [
        wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p, ctypes.c_void_p,
        wintypes.BOOL, wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR,
        ctypes.c_void_p, ctypes.c_void_p,
    ]
    _kernel32.CreateProcessW.restype = wintypes.BOOL
    _kernel32.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    _kernel32.OpenEventW.restype = wintypes.HANDLE
    _kernel32.SetEvent.argtypes = [wintypes.HANDLE]
    _kernel32.SetEvent.restype = wintypes.BOOL
    _kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    _kernel32.WaitForSingleObject.restype = wintypes.DWORD
    _kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _kernel32.TerminateProcess.restype = wintypes.BOOL

    def _make_kill_on_close_job() -> wintypes.HANDLE | None:
        """Anonymous Job Object that kills its members when the last handle
        (ours) closes — which happens when we exit, clean or crashed."""
        job = _kernel32.CreateJobObjectW(None, None)
        if not job:
            logger.warning("CreateJobObject failed: err=%d", ctypes.get_last_error())
            return None
        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not _kernel32.SetInformationJobObject(
            job, _JobObjectExtendedLimitInformation,
            ctypes.byref(info), ctypes.sizeof(info),
        ):
            logger.warning("SetInformationJobObject failed: err=%d", ctypes.get_last_error())
            _kernel32.CloseHandle(job)
            return None
        return job


def is_external_trackir_running() -> bool:
    """True if a different (non-our) TrackIR.exe is already running.

    We can't perfectly distinguish ours from a real one by process name
    alone — both would be `TrackIR.exe`. We use the executable path: if any
    `TrackIR.exe` process is running from a directory other than our
    bundled bin dir, we consider that external."""
    if not _is_windows():
        return False
    try:
        import psutil
    except ImportError:
        # Without psutil we can't enumerate; assume nothing external.
        return False

    our_path = dummy_path().resolve()
    for proc in psutil.process_iter(["name", "exe"]):
        try:
            name = proc.info.get("name")
            if not name or name.lower() != DUMMY_NAME.lower():
                continue
            exe = proc.info.get("exe")
            if not exe:
                continue
            if Path(exe).resolve() != our_path:
                logger.info("External TrackIR.exe already running at %s; yielding.", exe)
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            continue
    return False


class TrackIRShim:
    """Manages our bundled TrackIR.exe child process.

    Lifecycle:
        shim = TrackIRShim()
        shim.start()      # spawns helper if no external one already exists
        shim.stop()       # idempotent, asks it to exit, then kills if slow
    """

    def __init__(self) -> None:
        self._proc_handle: int | None = None
        self._pid: int | None = None
        self._yielded_to_external = False
        self._job_handle: int | None = None

    @property
    def is_running(self) -> bool:
        if self._proc_handle is None or sys.platform != "win32":
            return False
        return _kernel32.WaitForSingleObject(self._proc_handle, 0) == _WAIT_TIMEOUT

    @property
    def pid(self) -> int | None:
        return self._pid

    def start(self) -> None:
        if not _is_windows():
            return
        if self.is_running:
            return
        if is_external_trackir_running():
            self._yielded_to_external = True
            return

        path = dummy_path()
        if not path.exists():
            logger.warning(
                "TrackIR.exe helper missing at %s — OpenFOV install may be "
                "incomplete, or your antivirus may have quarantined it "
                "(it is a known false positive). Games that require a "
                "running TrackIR.exe won't initialize head tracking.",
                path,
            )
            logger.debug(
                "Dev-mode hint: run npclient-vendor/build.ps1 to build the "
                "helper into resources/bin/."
            )
            return

        # Job first: we want it to exist before the child does, so the
        # attribute list can bind the child to it atomically at creation.
        self._job_handle = _make_kill_on_close_job()

        if not self._spawn_with_job(path):
            self._close_job()
            return

        logger.info("Launched bundled TrackIR.exe (pid %s)", self._pid)

    # -- creation ------------------------------------------------------

    def _spawn_with_job(self, path: Path) -> bool:
        """CreateProcess with the Job Object attached via the attribute
        list, so the child is in the job before its first instruction runs.
        No CREATE_SUSPENDED, no thread enumeration, no ResumeThread."""
        size = ctypes.c_size_t(0)
        # First call always "fails" with ERROR_INSUFFICIENT_BUFFER; it is
        # how you learn the required size.
        _kernel32.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
        attr_buf = ctypes.create_string_buffer(size.value)
        attr_list = ctypes.cast(attr_buf, ctypes.c_void_p)

        use_attrs = self._job_handle is not None
        if use_attrs:
            if not _kernel32.InitializeProcThreadAttributeList(
                attr_list, 1, 0, ctypes.byref(size)
            ):
                logger.warning(
                    "InitializeProcThreadAttributeList failed: err=%d",
                    ctypes.get_last_error(),
                )
                use_attrs = False
            else:
                job = wintypes.HANDLE(self._job_handle)
                if not _kernel32.UpdateProcThreadAttribute(
                    attr_list, 0, _PROC_THREAD_ATTRIBUTE_JOB_LIST,
                    ctypes.byref(job), ctypes.sizeof(job), None, None,
                ):
                    logger.warning(
                        "UpdateProcThreadAttribute(JOB_LIST) failed: err=%d",
                        ctypes.get_last_error(),
                    )
                    _kernel32.DeleteProcThreadAttributeList(attr_list)
                    use_attrs = False

        si = _STARTUPINFOEXW()
        si.StartupInfo.cb = ctypes.sizeof(_STARTUPINFOEXW)
        flags = _CREATE_NO_WINDOW
        if use_attrs:
            si.lpAttributeList = attr_list
            flags |= _EXTENDED_STARTUPINFO_PRESENT

        pi = _PROCESS_INFORMATION()
        try:
            ok = _kernel32.CreateProcessW(
                str(path), None, None, None, False, flags,
                None, str(path.parent), ctypes.byref(si), ctypes.byref(pi),
            )
            if not ok:
                logger.error(
                    "Failed to launch TrackIR.exe: CreateProcess err=%d",
                    ctypes.get_last_error(),
                )
                return False
        finally:
            if use_attrs:
                _kernel32.DeleteProcThreadAttributeList(attr_list)

        # We don't need the thread handle; the process handle is our
        # liveness token.
        _kernel32.CloseHandle(pi.hThread)
        self._proc_handle = pi.hProcess
        self._pid = pi.dwProcessId

        if not use_attrs:
            logger.warning(
                "TrackIR.exe started without Job Object containment; it may "
                "outlive OpenFOV if we crash. pid=%s", self._pid,
            )
        return True

    # -- shutdown ------------------------------------------------------

    def stop(self, timeout: float = 2.0) -> None:
        if not _is_windows() or self._proc_handle is None:
            self._close_job()
            self._proc_handle = None
            self._pid = None
            return

        if self.is_running and not self._request_graceful_exit(timeout):
            logger.debug("TrackIR.exe didn't exit on request; terminating.")
            _kernel32.TerminateProcess(self._proc_handle, 0)
            _kernel32.WaitForSingleObject(self._proc_handle, 1000)

        _kernel32.CloseHandle(self._proc_handle)
        self._proc_handle = None
        self._pid = None
        # Closing the job handle would also kill any survivors. Doing it
        # explicitly tightens timing and frees the kernel object.
        self._close_job()

    def _request_graceful_exit(self, timeout: float) -> bool:
        """Signal the named event the helper waits on. Returns True if it
        exited within `timeout`."""
        ev = _kernel32.OpenEventW(_EVENT_MODIFY_STATE, False, SHUTDOWN_EVENT_NAME)
        if not ev:
            # Older helper build without the event, or it never got that
            # far. Not an error — caller falls back to terminate.
            return False
        try:
            if not _kernel32.SetEvent(ev):
                return False
        finally:
            _kernel32.CloseHandle(ev)
        return (
            _kernel32.WaitForSingleObject(self._proc_handle, int(timeout * 1000))
            == _WAIT_OBJECT_0
        )

    def _close_job(self) -> None:
        if self._job_handle is None or sys.platform != "win32":
            return
        _kernel32.CloseHandle(self._job_handle)
        self._job_handle = None

    def __enter__(self) -> TrackIRShim:
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.stop()


__all__ = [
    "DUMMY_NAME",
    "SHUTDOWN_EVENT_NAME",
    "TrackIRShim",
    "dummy_path",
    "is_external_trackir_running",
]
