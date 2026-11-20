# -*- coding: utf-8 -*-
"""Claude·Codex CLI를 프로젝트 폴더의 PowerShell 7 콘솔로 그대로 연다.

앱 안의 AI 탭은 도구를 전부 끈 채 CLI를 돌려 터미널보다 좁았다. 여기서는
설치된 `claude`·`codex`를 터미널과 똑같이 실행만 한다. 버전·옵션을 앱이 쥐지
않으므로 CLI를 업데이트하면 다음에 여는 콘솔부터 새 버전이다.

앱이 관리자 권한이면 자식 콘솔도 관리자가 된다. 그래서 「일반」은 탐색기(셸)의
토큰으로 낮춰 연다.
"""
from __future__ import annotations

import base64
import ctypes
import shutil
import subprocess
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROVIDERS = ("claude", "codex")
CREATE_NEW_CONSOLE = 0x00000010


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


def _pwsh() -> str:
    path = shutil.which("pwsh")
    if not path:
        raise FileNotFoundError("PowerShell 7(pwsh)을 찾을 수 없습니다.")
    return path


def _quote(text: str) -> str:
    """PowerShell 작은따옴표 문자열. 안의 ' 만 두 번 쓰면 된다."""
    return "'" + text.replace("'", "''") + "'"


def build_script(provider: str, prompt: str = "", cwd: Path = ROOT) -> str:
    if provider not in PROVIDERS:
        raise ValueError("지원하지 않는 AI입니다.")
    command = provider + (" " + _quote(prompt) if prompt.strip() else "")
    return f"Set-Location -LiteralPath {_quote(str(cwd))}; {command}"


def build_arguments(script: str, *, hidden: bool = False) -> list[str]:
    # -EncodedCommand: 종목명·질문이 셸 문자열로 해석될 일이 없다.
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    args = ["-NoLogo", "-NoExit"]
    if hidden:
        args += ["-WindowStyle", "Hidden"]
    return args + ["-EncodedCommand", encoded]


def _command_line(exe: str, args: list[str]) -> str:
    return subprocess.list2cmdline([exe, *args])


class _STARTUPINFO(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR),
                ("lpDesktop", wintypes.LPWSTR), ("lpTitle", wintypes.LPWSTR),
                ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
                ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD),
                ("dwXCountChars", wintypes.DWORD), ("dwYCountChars", wintypes.DWORD),
                ("dwFillAttribute", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD),
                ("lpReserved2", ctypes.c_void_p), ("hStdInput", wintypes.HANDLE),
                ("hStdOutput", wintypes.HANDLE), ("hStdError", wintypes.HANDLE)]


class _PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE),
                ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD)]


def _spawn_as_shell_user(exe: str, args: list[str], cwd: Path) -> None:
    """관리자 프로세스에서 탐색기의 (낮은) 토큰으로 새 콘솔을 띄운다."""
    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    advapi32 = ctypes.windll.advapi32
    for fn in (kernel32.OpenProcess, user32.GetShellWindow):
        fn.restype = wintypes.HANDLE
    hwnd = user32.GetShellWindow()
    if not hwnd:
        raise OSError("탐색기 창을 찾을 수 없어 일반 권한으로 열 수 없습니다.")
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    process = kernel32.OpenProcess(0x1000, False, pid.value)  # QUERY_LIMITED_INFORMATION
    if not process:
        raise ctypes.WinError()
    token, primary = wintypes.HANDLE(), wintypes.HANDLE()
    try:
        if not advapi32.OpenProcessToken(process, 0x0002, ctypes.byref(token)):  # DUPLICATE
            raise ctypes.WinError()
        # TOKEN_ASSIGN_PRIMARY|DUPLICATE|QUERY|ADJUST_DEFAULT|ADJUST_SESSIONID
        if not advapi32.DuplicateTokenEx(token, 0x0001 | 0x0002 | 0x0008 | 0x0080 | 0x0100,
                                         None, 2, 1, ctypes.byref(primary)):
            raise ctypes.WinError()
        info = _STARTUPINFO()
        info.cb = ctypes.sizeof(info)
        out = _PROCESS_INFORMATION()
        if not advapi32.CreateProcessWithTokenW(
                primary, 0, exe, _command_line(exe, args), CREATE_NEW_CONSOLE,
                None, str(cwd), ctypes.byref(info), ctypes.byref(out)):
            raise ctypes.WinError()
        kernel32.CloseHandle(out.hProcess)
        kernel32.CloseHandle(out.hThread)
    finally:
        for handle in (token, primary, process):
            if handle:
                kernel32.CloseHandle(handle)


def _spawn_elevated(exe: str, args: list[str], cwd: Path) -> None:
    """일반 프로세스에서 UAC로 관리자 콘솔을 띄운다(사용자가 취소하면 오류)."""
    result = ctypes.windll.shell32.ShellExecuteW(
        None, "runas", exe, subprocess.list2cmdline(args), str(cwd), 1)
    if result <= 32:
        raise OSError("관리자 권한 요청이 취소되었거나 실패했습니다.")


def spawn(script: str, *, admin: bool, hidden: bool = False, cwd: Path = ROOT) -> str:
    """script를 PowerShell 7 새 콘솔로 연다. 실제로 쓴 방식을 돌려준다."""
    exe, args = _pwsh(), build_arguments(script, hidden=hidden)
    running_admin = is_admin()
    if admin and not running_admin:
        _spawn_elevated(exe, args, cwd)
        return "uac"
    if not admin and running_admin:
        _spawn_as_shell_user(exe, args, cwd)
        return "lowered"
    subprocess.Popen([exe, *args], cwd=str(cwd), creationflags=CREATE_NEW_CONSOLE)
    return "direct"


def open_console(provider: str, *, admin: bool, prompt: str = "", cwd: Path = ROOT) -> str:
    if shutil.which(provider) is None:
        raise FileNotFoundError(f"{provider} CLI를 찾을 수 없습니다. 터미널에서 설치를 확인하세요.")
    return spawn(build_script(provider, prompt, cwd), admin=admin, cwd=cwd)
