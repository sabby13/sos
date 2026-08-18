"""Windows adapter, built on ctypes so OpenJarvis needs no pywin32 dependency.

Covers: Start Menu application discovery, window enumeration/focus/close, Explorer
integration, Recycle Bin deletes, and MCI audio playback for the TTS engines.
"""

from __future__ import annotations

import ctypes
import functools
import os
import subprocess
import threading
from ctypes import wintypes
from pathlib import Path

from ..console import get_logger
from .base import AppInfo, PlatformAdapter, WindowInfo

log = get_logger("platform.windows")

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
winmm = ctypes.WinDLL("winmm", use_last_error=True)

# ShowWindow commands
SW_HIDE, SW_SHOWNORMAL, SW_SHOWMINIMIZED, SW_MAXIMIZE, SW_RESTORE = 0, 1, 2, 3, 9
WM_CLOSE = 0x0010
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

# SHFileOperation
FO_DELETE = 0x0003
FOF_SILENT = 0x0004
FOF_NOCONFIRMATION = 0x0010
FOF_ALLOWUNDO = 0x0040  # this is the flag that means "Recycle Bin" rather than "gone"
FOF_NOERRORUI = 0x0400

WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


class SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("wFunc", wintypes.UINT),
        ("pFrom", wintypes.LPCWSTR),
        ("pTo", wintypes.LPCWSTR),
        ("fFlags", ctypes.c_uint16),
        ("fAnyOperationsAborted", wintypes.BOOL),
        ("hNameMappings", ctypes.c_void_p),
        ("lpszProgressTitle", wintypes.LPCWSTR),
    ]


# Windows that exist but are not things a user thinks of as "open windows".
_SHELL_WINDOW_TITLES = {
    "Program Manager",
    "Windows Input Experience",
    "Settings",
    "Microsoft Text Input Application",
    "NVIDIA GeForce Overlay",
}


class WindowsAdapter(PlatformAdapter):
    name = "windows"

    def __init__(self) -> None:
        self._mci_alias: str | None = None
        self._mci_lock = threading.Lock()

    # ------------------------------------------------------------------ apps

    def list_apps(self) -> list[AppInfo]:
        return _discover_apps()

    def refresh_apps(self) -> list[AppInfo]:
        _discover_apps.cache_clear()
        return _discover_apps()

    def launch(self, target: str, args: list[str] | None = None) -> int | None:
        args = args or []
        path = Path(os.path.expandvars(target))

        # Shortcuts and documents: let the shell resolve them. os.startfile cannot take
        # arguments, so anything with args goes through the explorer/cmd path instead.
        if path.suffix.lower() in {".lnk", ".url"} and not args:
            os.startfile(str(path))
            return None

        if path.suffix.lower() in {".lnk", ".url"} and args:
            # `explorer shortcut.lnk args` is the reliable way to pass arguments to a
            # shortcut without a COM dependency.
            subprocess.Popen(["explorer.exe", str(path), *args])
            return None

        exe = str(path) if path.exists() else (self.which(target) or target)
        try:
            proc = subprocess.Popen(
                [exe, *args],
                close_fds=True,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000008,  # DETACHED
            )
            return proc.pid
        except (OSError, ValueError):
            # Not a plain executable - could be a URI (ms-settings:) or a shell verb.
            os.startfile(target)
            return None

    # --------------------------------------------------------------- windows

    def list_windows(self) -> list[WindowInfo]:
        found: list[WindowInfo] = []

        def callback(hwnd: int, _lparam: int) -> bool:
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length == 0:
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            title = buf.value.strip()
            if not title or title in _SHELL_WINDOW_TITLES:
                return True
            found.append(
                WindowInfo(
                    handle=hwnd,
                    title=title,
                    process=_process_name(hwnd),
                    minimized=bool(user32.IsIconic(hwnd)),
                )
            )
            return True

        user32.EnumWindows(WNDENUMPROC(callback), 0)
        return found

    def focus_window(self, handle: int) -> bool:
        if not user32.IsWindow(handle):
            return False
        if user32.IsIconic(handle):
            user32.ShowWindow(handle, SW_RESTORE)

        # SetForegroundWindow is refused unless the calling thread owns the foreground
        # window. Attaching to that thread's input queue is the standard workaround.
        fg = user32.GetForegroundWindow()
        target_tid = user32.GetWindowThreadProcessId(handle, None)
        current_tid = kernel32.GetCurrentThreadId()
        fg_tid = user32.GetWindowThreadProcessId(fg, None) if fg else 0

        attached = []
        for tid in {target_tid, fg_tid} - {current_tid, 0}:
            if user32.AttachThreadInput(current_tid, tid, True):
                attached.append(tid)
        try:
            user32.BringWindowToTop(handle)
            ok = bool(user32.SetForegroundWindow(handle))
        finally:
            for tid in attached:
                user32.AttachThreadInput(current_tid, tid, False)
        return ok

    def close_window(self, handle: int) -> bool:
        if not user32.IsWindow(handle):
            return False
        # WM_CLOSE is the polite request - the app can still prompt to save.
        return bool(user32.PostMessageW(handle, WM_CLOSE, 0, 0))

    def set_window_state(self, handle: int, state: str) -> bool:
        commands = {"minimize": SW_SHOWMINIMIZED, "maximize": SW_MAXIMIZE, "restore": SW_RESTORE}
        if state not in commands:
            return False
        if not user32.IsWindow(handle):
            return False
        user32.ShowWindow(handle, commands[state])
        return True

    # ----------------------------------------------------------------- files

    def reveal(self, path: Path) -> bool:
        if path.is_dir():
            subprocess.Popen(["explorer.exe", str(path)])
        else:
            # /select, highlights the file inside its parent folder.
            subprocess.Popen(["explorer.exe", "/select,", str(path)])
        return True

    def open_with_default_app(self, path: Path) -> bool:
        os.startfile(str(path))
        return True

    def move_to_trash(self, path: Path) -> bool:
        # pFrom is a double-null-terminated list of paths.
        op = SHFILEOPSTRUCTW(
            hwnd=None,
            wFunc=FO_DELETE,
            pFrom=f"{path}\0\0",
            pTo=None,
            fFlags=FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI,
            fAnyOperationsAborted=False,
            hNameMappings=None,
            lpszProgressTitle=None,
        )
        result = shell32.SHFileOperationW(ctypes.byref(op))
        if result != 0 or op.fAnyOperationsAborted:
            log.debug("SHFileOperationW returned %s for %s", result, path)
            return False
        return True

    # ----------------------------------------------------------------- audio

    def play_audio_file(self, path: Path, *, block: bool = True) -> None:
        """Play through MCI, which handles both wav and mp3 with no extra dependency."""
        alias = f"jarvis{os.getpid()}"
        with self._mci_lock:
            self._mci_command(f'close {alias}', check=False)
            self._mci_command(f'open "{path}" alias {alias}')
            self._mci_alias = alias
        try:
            # `wait` blocks this thread; stop_audio() can still interrupt from another.
            self._mci_command(f"play {alias}{' wait' if block else ''}", check=False)
        finally:
            if block:
                with self._mci_lock:
                    self._mci_command(f"close {alias}", check=False)
                    self._mci_alias = None

    def stop_audio(self) -> None:
        with self._mci_lock:
            if self._mci_alias:
                self._mci_command(f"stop {self._mci_alias}", check=False)
                self._mci_command(f"close {self._mci_alias}", check=False)
                self._mci_alias = None

    @staticmethod
    def _mci_command(command: str, *, check: bool = True) -> str:
        buf = ctypes.create_unicode_buffer(512)
        code = winmm.mciSendStringW(command, buf, 512, None)
        if code != 0 and check:
            err = ctypes.create_unicode_buffer(256)
            winmm.mciGetErrorStringW(code, err, 256)
            raise OSError(f"MCI command {command!r} failed: {err.value}")
        return buf.value


def _process_name(hwnd: int) -> str:
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(260)
        buf = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return Path(buf.value).name
        return ""
    finally:
        kernel32.CloseHandle(handle)


@functools.lru_cache(maxsize=1)
def _discover_apps() -> list[AppInfo]:
    """Scan both Start Menus for shortcuts, then anything executable on PATH.

    Cached because a full scan touches a few thousand directory entries; call
    ``WindowsAdapter.refresh_apps()`` after installing something new.
    """
    apps: dict[str, AppInfo] = {}

    start_menus = [
        Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData"))
        / "Microsoft/Windows/Start Menu/Programs",
        Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs",
    ]
    for root in start_menus:
        if not root.is_dir():
            continue
        try:
            for entry in root.rglob("*"):
                if entry.suffix.lower() not in {".lnk", ".url"}:
                    continue
                key = entry.stem.lower()
                # First writer wins, so the machine-wide menu beats the per-user one.
                apps.setdefault(key, AppInfo(entry.stem, str(entry), "start-menu"))
        except OSError as exc:
            log.debug("could not scan %s: %s", root, exc)

    for command in (
        "code", "python", "pwsh", "powershell", "cmd", "explorer", "notepad",
        "chrome", "msedge", "firefox", "git", "node", "wt",
    ):
        resolved = None
        try:
            import shutil

            resolved = shutil.which(command)
        except OSError:
            pass
        if resolved:
            apps.setdefault(command, AppInfo(command, resolved, "path"))

    return sorted(apps.values(), key=lambda a: a.name.lower())
