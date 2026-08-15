/* TrackIR.exe presence shim
 *
 * Some games (Falcon BMS, parts of MSFS) check whether a process named
 * `TrackIR.exe` is running before they initialize TrackIR head-tracking,
 * regardless of whether NPClient.dll itself loaded successfully. OpenFOV
 * launches this binary while tracking is active so that check passes.
 *
 * Implementation notes -- these matter, and are deliberate:
 *
 * Earlier revisions were literally `for(;;) Sleep(INFINITE);`. That is a
 * ~15 KB stripped, unsigned executable whose entry point immediately sleeps
 * forever and does nothing else. To a behavioural classifier that is
 * indistinguishable from a sandbox-evading dropper stub, and Microsoft
 * Defender duly flagged the shipped binary as `Trojan:Win32/Ravartar!rfn`
 * (severity Severe) and quarantined it out of the install directory --
 * which silently broke head tracking for the games that need it.
 *
 * So instead we are shaped like an ordinary Win32 application:
 *   - a message-only window (HWND_MESSAGE: no pixels, no GDI, no taskbar
 *     entry, but a real window with a real class and a real WndProc),
 *   - a standard MsgWaitForMultipleObjectsEx pump rather than a raw sleep,
 *   - a named manual-reset event so the parent can ask us to exit cleanly
 *     instead of resorting to TerminateProcess,
 *   - a version resource (see trackir.rc) and unstripped symbols, because
 *     "no version info, no symbols, tiny" is itself a heuristic signal.
 *
 * The process still costs no measurable CPU: the pump blocks in the kernel
 * until a message or the shutdown event arrives.
 *
 * Build: -mwindows so no console appears. See build.ps1.
 */

#include <windows.h>

/* Parent (OpenFOV) opens this event and signals it to request a clean exit.
   Local\ scopes it to the session, so two users can each run their own. */
#define SHUTDOWN_EVENT_NAME L"Local\\OpenFOV.TrackIRShim.Shutdown"
#define WINDOW_CLASS_NAME   L"OpenFOV.TrackIRShim"

static LRESULT CALLBACK WndProc(HWND hwnd, UINT msg, WPARAM wp, LPARAM lp)
{
    switch (msg)
    {
    case WM_CLOSE:
        DestroyWindow(hwnd);
        return 0;
    case WM_DESTROY:
        PostQuitMessage(0);
        return 0;
    case WM_ENDSESSION:
        /* Windows is shutting down / logging off -- leave promptly. */
        PostQuitMessage(0);
        return 0;
    default:
        break;
    }
    return DefWindowProcW(hwnd, msg, wp, lp);
}

int WINAPI wWinMain(HINSTANCE hInstance, HINSTANCE hPrevInstance,
                    PWSTR lpCmdLine, int nShowCmd)
{
    WNDCLASSEXW wc;
    HWND hwnd;
    HANDLE hShutdown;
    MSG msg;
    BOOL running = TRUE;

    (void)hPrevInstance; (void)lpCmdLine; (void)nShowCmd;

    hShutdown = CreateEventW(NULL, TRUE, FALSE, SHUTDOWN_EVENT_NAME);
    /* A NULL handle here is survivable: we simply rely on the window
       message path (and, failing that, the parent's Job Object) to exit. */

    ZeroMemory(&wc, sizeof(wc));
    wc.cbSize        = sizeof(wc);
    wc.lpfnWndProc   = WndProc;
    wc.hInstance     = hInstance;
    wc.lpszClassName = WINDOW_CLASS_NAME;
    if (!RegisterClassExW(&wc))
    {
        if (hShutdown) CloseHandle(hShutdown);
        return 1;
    }

    /* HWND_MESSAGE => message-only window: never rendered, never enumerated
       as a top-level window, but gives us a normal message queue. */
    hwnd = CreateWindowExW(0, WINDOW_CLASS_NAME, WINDOW_CLASS_NAME, 0,
                           0, 0, 0, 0, HWND_MESSAGE, NULL, hInstance, NULL);
    if (!hwnd)
    {
        if (hShutdown) CloseHandle(hShutdown);
        return 1;
    }

    while (running)
    {
        DWORD wait;
        DWORD count = hShutdown ? 1 : 0;
        HANDLE handles[1];
        handles[0] = hShutdown;

        /* Block in the kernel until either the shutdown event fires or a
           window message arrives. No polling, no timed sleeping. */
        wait = MsgWaitForMultipleObjectsEx(count, count ? handles : NULL,
                                           INFINITE, QS_ALLINPUT, 0);

        if (count && wait == WAIT_OBJECT_0)
        {
            running = FALSE;
            break;
        }

        while (PeekMessageW(&msg, NULL, 0, 0, PM_REMOVE))
        {
            if (msg.message == WM_QUIT)
            {
                running = FALSE;
                break;
            }
            TranslateMessage(&msg);
            DispatchMessageW(&msg);
        }
    }

    DestroyWindow(hwnd);
    UnregisterClassW(WINDOW_CLASS_NAME, hInstance);
    if (hShutdown) CloseHandle(hShutdown);
    return 0;
}
