# -*- coding: utf-8 -*-
"""AI 콘솔 버튼: 명령 조립·권한 선택·버튼 연결을 확인한다.

기본 실행은 콘솔을 열지 않는다. `--live`는 보이지 않는 PowerShell 7을 실제로
띄워 한글·작은따옴표 질문과 권한 상태를 임시 파일로 받아 본다.
관리자 PowerShell에서 `--live`를 돌리면 「관리자 앱 → 일반 권한」 경로까지 본다.
"""
import base64
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import ai_console  # noqa: E402


def decode(args):
    return base64.b64decode(args[args.index("-EncodedCommand") + 1]).decode("utf-16-le")


def demo_script():
    script = ai_console.build_script("claude", "다음 종목을 분석해 주세요: 금양그린파워, A'B", Path("D:/x y"))
    assert script == ("Set-Location -LiteralPath 'D:\\x y'; "
                      "claude '다음 종목을 분석해 주세요: 금양그린파워, A''B'"), script
    assert ai_console.build_script("codex").endswith("; codex")
    try:
        ai_console.build_script("evil; calc")
    except ValueError:
        pass
    else:
        raise AssertionError("허용되지 않은 AI 이름을 막지 못함")
    args = ai_console.build_arguments(script, hidden=True)
    assert decode(args) == script and "-NoExit" in args and "Hidden" in args
    # 질문이 명령줄에 그대로 실리지 않는다(셸 해석 방지).
    assert "금양" not in " ".join(args)
    print("ok (명령 조립·인용·인코딩)")


def demo_routes():
    calls = []
    real = (ai_console._spawn_elevated, ai_console._spawn_as_shell_user,
            ai_console.subprocess.Popen, ai_console.is_admin, ai_console._pwsh)
    ai_console._spawn_elevated = lambda *a: calls.append("uac")
    ai_console._spawn_as_shell_user = lambda *a: calls.append("lowered")
    ai_console.subprocess.Popen = lambda *a, **k: calls.append("direct")
    ai_console._pwsh = lambda: "pwsh.exe"
    try:
        for running, want, expect in ((False, False, "direct"), (False, True, "uac"),
                                      (True, True, "direct"), (True, False, "lowered")):
            ai_console.is_admin = lambda running=running: running
            calls.clear()
            assert ai_console.spawn("1", admin=want) == expect and calls == [expect], (running, want, calls)
    finally:
        (ai_console._spawn_elevated, ai_console._spawn_as_shell_user,
         ai_console.subprocess.Popen, ai_console.is_admin, ai_console._pwsh) = real
    print("ok (앱 권한 × 요청 권한 4경로)")


def demo_tab():
    from PySide6.QtWidgets import QApplication, QMessageBox
    from ui.ai_assistant_tab import AiAssistantWidget
    app = QApplication.instance() or QApplication([])
    with tempfile.TemporaryDirectory(prefix="trading_ai_console_") as tmp:
        widget = AiAssistantWidget(db_path=Path(tmp) / "none.db", session_path=Path(tmp) / "s.db")
        assert [b.text() for b in widget.console_buttons] == ["Claude 콘솔", "Codex 콘솔"]
        assert [widget.console_admin.itemData(i) for i in range(2)] == [False, True]
        assert widget.console_admin.currentData() is False, "기본은 일반 권한"
        seen, warned = [], []
        real, real_warn = ai_console.open_console, QMessageBox.warning
        ai_console.open_console = lambda provider, **k: seen.append((provider, k))
        QMessageBox.warning = staticmethod(lambda *a: warned.append(a[2]))
        try:
            widget.console_buttons[0].click()
            widget.targets.setText("005930, 000660")
            widget.console_admin.setCurrentIndex(1)
            widget.console_buttons[1].click()
            ai_console.open_console = lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("없음"))
            widget.console_buttons[0].click()
        finally:
            ai_console.open_console, QMessageBox.warning = real, real_warn
        assert seen[0] == ("claude", {"admin": False, "prompt": ""}), seen[0]
        assert seen[1] == ("codex", {"admin": True,
                                     "prompt": "다음 종목을 분석해 주세요: 005930, 000660"}), seen[1]
        assert warned == ["없음"], warned
        widget.deleteLater()
        app.processEvents()
    print("ok (탭 버튼·권한 선택·오류 안내)")


def live():
    with tempfile.TemporaryDirectory(prefix="trading_console_live_") as tmp:
        out = Path(tmp) / "out.txt"
        prompt = "한글 'quote' 테스트"
        body = (f"$p=[Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent();"
                f"Set-Content -LiteralPath '{out}' -Encoding utf8 -Value ('admin=' + $p.IsInRole('Administrator') + ' prompt=' + {ai_console._quote(prompt)}); exit")
        for want in (False, True) if ai_console.is_admin() else (False,):
            out.unlink(missing_ok=True)
            route = ai_console.spawn(body, admin=want, hidden=True)
            for _ in range(100):
                if out.exists() and out.stat().st_size:
                    break
                time.sleep(0.1)
            text = out.read_text(encoding="utf-8").strip() if out.exists() else "(no output)"
            print(f"요청 admin={want} 경로={route} → {text}")
            assert f"admin={want}" in text and prompt in text, text


if __name__ == "__main__":
    demo_script()
    demo_routes()
    demo_tab()
    if "--live" in sys.argv:
        live()
