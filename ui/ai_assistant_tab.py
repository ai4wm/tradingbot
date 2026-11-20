# -*- coding: utf-8 -*-
"""앱 자료와 실제 CLI를 연결하는 AI 분석 탭."""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, QRunnable, QThreadPool, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor, QDesktopServices, QFont, QKeySequence, QPalette, QShortcut,
    QTextCharFormat, QTextCursor, QTextDocument,
)
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QSplitter, QTextBrowser,
    QVBoxLayout, QWidget,
)

import ai_console

from ai_cli_runner import CliRunner
from ai_context import MARKET_DB, MAX_STOCKS, build_context, compose_prompt, read_catalog, redact, valid_code
from ai_sessions import SESSION_DB, SessionStore


QUESTIONS = {
    "stock": "선택 종목의 최근 재료, 일봉과 거래량, 강점과 약점, 다음 확인 사항을 분석해 주세요.",
    "compare": "오늘 시장의 대장 후보 3종목을 골라 비교해 주세요. 누가 가장 강한지와 그 이유, 주의할 점을 쉽게 설명해 주세요.",
    "market": "첨부된 시장 자료와 관심종목에서 확인되는 시장 흐름과 강한 종목을 분석해 주세요. 오래된 자료는 현재 장세로 말하지 마세요.",
    "orders": "선택 종목의 주문 감사 기록을 시간 순서로 정리하고, 접수·체결·취소와 자동매매가 동작한 이유를 설명해 주세요. 기록이 없는 입력값은 추측하지 마세요.",
}


class _JobSignals(QObject):
    ready = Signal(int, object, str)


class _ContextJob(QRunnable):
    def __init__(self, serial, kwargs):
        super().__init__()
        self.serial, self.kwargs = serial, kwargs
        self.signals = _JobSignals()

    def run(self):
        try:
            self.signals.ready.emit(self.serial, build_context(**self.kwargs), "")
        except Exception as error:
            self.signals.ready.emit(self.serial, None, redact(str(error)))


class AnswerBrowser(QTextBrowser):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setOpenLinks(False)
        self.anchorClicked.connect(self._open_link)

    def loadResource(self, resource_type, url):
        # 답변이 삽입한 로컬 파일/원격 이미지를 자동으로 읽지 않는다.
        return None

    @staticmethod
    def _open_link(url):
        if url.scheme().lower() in ("http", "https"):
            QDesktopServices.openUrl(url)


class AiAssistantWidget(QWidget):
    def __init__(self, parent=None, *, db_path=MARKET_DB, session_path=SESSION_DB,
                 log_path=None, runner=None, capture=None, selection=None, market_scan=None):
        super().__init__(parent)
        self.db_path = Path(db_path)
        self.log_path = Path(log_path) if log_path else Path(__file__).resolve().parents[1] / "bot.log"
        self.capture = capture or (lambda: {})
        self.selection = selection or (lambda: [])
        self.market_scan = market_scan
        self._scan_task = None
        self.runner = runner or CliRunner(self)
        self._session_id = ""
        self._messages = []
        self._active_message = None
        self._context = {}
        self._stream = ""
        self._serial = 0
        self._jobs = {}
        self._loading = False
        self._busy = False
        self._pending_request = None
        self._models = {"codex": [], "claude": []}
        self._store_error = ""
        self._work_started = None
        self._work_elapsed = 0
        self._last_signal = None
        self._phase = ""
        self._progress_log = []
        try:
            self.store = SessionStore(session_path)
        except Exception as error:
            self.store = None
            self._store_error = "대화 저장소를 열지 못했습니다: " + str(error)
        self._build()
        self.runner.text_changed.connect(self._text_changed)
        self.runner.status_changed.connect(self._record_stage)
        self.runner.signal_received.connect(self._signal_received)
        self.runner.model_changed.connect(self._model_resolved)
        self.runner.completed.connect(self._completed)
        self.runner.failed.connect(self._failed)
        self.runner.models_ready.connect(self._models_ready)
        self._paint_timer = QTimer(self)
        self._paint_timer.setSingleShot(True)
        self._paint_timer.timeout.connect(self._paint)
        self._activity_timer = QTimer(self)
        self._activity_timer.setInterval(1000)
        self._activity_timer.timeout.connect(self._update_progress)
        app = QApplication.instance()
        if app:
            app.aboutToQuit.connect(self.shutdown)
        self._refresh_sessions()
        if self.history.count():
            self.history.setCurrentRow(0)
        else:
            self._paint()
        if self._store_error:
            self.status.setText(self._store_error)
            self.send_button.setEnabled(False)

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.Type.PaletteChange and hasattr(self, "_paint_timer"):
            self._paint_timer.start(0)

    def _open_console(self, provider: str):
        targets = self.targets.text().strip()
        prompt = f"다음 종목을 분석해 주세요: {targets}" if targets else ""
        try:
            ai_console.open_console(
                provider, admin=bool(self.console_admin.currentData()), prompt=prompt)
        except (OSError, ValueError) as error:
            QMessageBox.warning(self, "AI 콘솔", str(error))

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        top = QHBoxLayout()
        self.provider = QComboBox()
        self.provider.addItem("Codex", "codex")
        self.provider.addItem("Claude", "claude")
        self.model = QComboBox()
        self.model.setEditable(True)
        self.model.setMinimumWidth(170)
        self.model.addItem("기본 모델", "")
        self.model.setToolTip("연결 확인으로 Codex 모델 목록을 불러옵니다. 모델 이름을 직접 입력할 수도 있습니다.")
        self.probe_button = QPushButton("연결 확인")
        self.probe_button.clicked.connect(self._probe)
        self.new_button = QPushButton("새 대화")
        self.new_button.clicked.connect(self._new_chat)
        self.stop_button = QPushButton("중지")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self._stop)
        self.retry_button = QPushButton("다시 요청")
        self.retry_button.setToolTip("마지막 질문을 새로 준비한 자료와 함께 다시 보냅니다.")
        self.retry_button.setEnabled(False)
        self.retry_button.clicked.connect(self._retry_last)
        top.addWidget(QLabel("AI"))
        top.addWidget(self.provider)
        top.addWidget(QLabel("모델"))
        top.addWidget(self.model, 1)
        for button in (self.probe_button, self.new_button, self.retry_button, self.stop_button):
            top.addWidget(button)
        root.addLayout(top)
        console = QHBoxLayout()
        self.console_admin = QComboBox()
        self.console_admin.addItem("일반 권한", False)
        self.console_admin.addItem("관리자 권한", True)
        self.console_admin.setToolTip(
            "일반: 앱이 관리자여도 일반 권한으로 엽니다.\n"
            "관리자: 관리자 콘솔입니다. 파일 쓰기·명령 실행 제한이 없습니다.")
        self.console_buttons = []
        console.addWidget(QLabel("AI 콘솔"))
        for provider, label in (("claude", "Claude 콘솔"), ("codex", "Codex 콘솔")):
            button = QPushButton(label)
            button.setToolTip(
                f"프로젝트 폴더에서 {provider}를 PowerShell 7 터미널로 그대로 엽니다. "
                "분석 종목 칸이 차 있으면 그 종목을 첫 질문으로 미리 입력합니다.")
            button.clicked.connect(lambda _=False, name=provider: self._open_console(name))
            self.console_buttons.append(button)
            console.addWidget(button)
        console.addWidget(self.console_admin)
        console.addStretch(1)
        root.addLayout(console)
        targets = QHBoxLayout()
        self.targets = QLineEdit()
        self.targets.setPlaceholderText("종목코드·종목명 (여러 종목은 쉼표로 구분)")
        self.select_button = QPushButton("현재 선택")
        self.select_button.clicked.connect(lambda: self.set_targets(self.selection()))
        self.watch_button = QPushButton("관심목록 선택")
        self.watch_button.clicked.connect(self._choose_watchlist)
        targets.addWidget(QLabel("분석 종목"))
        targets.addWidget(self.targets, 1)
        targets.addWidget(self.select_button)
        targets.addWidget(self.watch_button)
        root.addLayout(targets)
        options = QHBoxLayout()
        self.attach = QCheckBox("종목·시장 자료 첨부")
        self.attach.setChecked(True)
        self.logs = QCheckBox("선택 종목 주문 기록")
        self.preview_button = QPushButton("첨부 자료 보기")
        self.preview_button.clicked.connect(lambda: self._prepare(preview=True))
        options.addWidget(self.attach)
        options.addWidget(self.logs)
        options.addStretch(1)
        options.addWidget(self.preview_button)
        root.addLayout(options)
        self.comparison_scope = QLabel()
        self.comparison_scope.setWordWrap(True)
        self.comparison_scope.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.comparison_scope.hide()
        root.addWidget(self.comparison_scope)
        self.status = QLabel("준비됨 · 저장된 앱 자료로 분석합니다. Ctrl+Enter로 전송")
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        status_row = QHBoxLayout()
        status_row.addWidget(self.status, 1)
        self.details_button = QPushButton("진행 기록")
        self.details_button.setCheckable(True)
        status_row.addWidget(self.details_button)
        root.addLayout(status_row)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setTextVisible(False)
        self.progress.setMaximumHeight(6)
        self.progress.hide()
        root.addWidget(self.progress)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setMaximumHeight(105)
        self.details.setPlaceholderText("요청을 보내면 실제 연결 단계와 시간이 표시됩니다.")
        self.details.hide()
        self.details_button.toggled.connect(self.details.setVisible)
        root.addWidget(self.details)
        split = QSplitter()
        left = QWidget()
        side = QVBoxLayout(left)
        side.setContentsMargins(0, 0, 0, 0)
        side.addWidget(QLabel("이전 대화"))
        self.history = QListWidget()
        self.history.setToolTip("각 대화는 최근 60개 메시지를 표시합니다. 전체 기록은 대화 DB에 보관합니다.")
        self.history.currentItemChanged.connect(self._load_session)
        side.addWidget(self.history)
        self.delete_button = QPushButton("선택 대화 삭제")
        self.delete_button.clicked.connect(self._delete_session)
        side.addWidget(self.delete_button)
        split.addWidget(left)
        self.answer = AnswerBrowser()
        split.addWidget(self.answer)
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setSizes([180, 720])
        root.addWidget(split, 1)
        actions = QHBoxLayout()
        self.action_buttons = []
        for kind, label in (("stock", "종목 분석"), ("compare", "대장 비교"),
                            ("market", "시장 흐름"), ("orders", "주문 점검")):
            button = QPushButton(label)
            if kind == "compare":
                button.setToolTip("오늘 KRX 상승률·거래대금 순위와 앱 화면에서 대장 후보를 새로 찾습니다. 이전 분석 종목은 사용하지 않습니다.")
            button.clicked.connect(lambda checked=False, k=kind: self._preset(k))
            actions.addWidget(button)
            self.action_buttons.append(button)
        root.addLayout(actions)
        bottom = QHBoxLayout()
        self.question = QPlainTextEdit()
        self.question.setPlaceholderText("질문을 입력하세요. 선택 종목과 필요한 자료를 함께 보냅니다.")
        self.question.setMaximumHeight(85)
        self.send_button = QPushButton("보내기")
        self.send_button.clicked.connect(lambda: self._prepare())
        bottom.addWidget(self.question, 1)
        bottom.addWidget(self.send_button)
        root.addLayout(bottom)
        self._send_shortcut = QShortcut(QKeySequence("Ctrl+Return"), self.question)
        self._send_shortcut.activated.connect(lambda: self._prepare())
        self.provider.currentIndexChanged.connect(self._provider_changed)

    def _requested_model(self):
        text = self.model.currentText().strip()
        if text == "기본 모델":
            return ""
        index = self.model.findText(text)
        return str(self.model.itemData(index) if index >= 0 else text)

    def _set_busy(self, on):
        if on and not self._busy:
            self._work_started = time.monotonic()
            self._work_elapsed = 0
            self._last_signal = None
            self._phase = ""
            self._progress_log = []
            self.details.clear()
            self._activity_timer.start()
        elif not on:
            if self._work_started is not None and self._busy:
                self._work_elapsed = time.monotonic() - self._work_started
            self._activity_timer.stop()
        self._busy = on
        self.progress.setVisible(on)
        for widget in (self.provider, self.model, self.probe_button, self.new_button,
                       self.targets, self.select_button, self.watch_button,
                       self.attach, self.logs, self.preview_button, self.history,
                       self.delete_button, self.send_button, *self.action_buttons):
            widget.setEnabled(not on and self.store is not None)
        self.stop_button.setEnabled(on)
        self.retry_button.setEnabled(not on and self.store is not None
                                     and any(m["role"] == "user" for m in self._messages))

    @staticmethod
    def _duration(seconds):
        seconds = max(0, int(seconds))
        return f"{seconds // 60:02}:{seconds % 60:02}"

    def _elapsed(self):
        if self._busy and self._work_started is not None:
            return time.monotonic() - self._work_started
        return self._work_elapsed

    def _record_stage(self, stage):
        stage = redact(stage)
        if stage != self._phase:
            self._phase = stage
            line = f"{self._duration(self._elapsed())} · {stage}"
            self._progress_log.append(line)
            self._progress_log = self._progress_log[-60:]
            self.details.setPlainText("\n".join(self._progress_log))
            self.details.verticalScrollBar().setValue(self.details.verticalScrollBar().maximum())
        self._update_progress()

    def _signal_received(self):
        self._last_signal = time.monotonic()

    def _model_resolved(self, model):
        for message in self._messages:
            if message["id"] == self._active_message:
                meta = json.loads(message.get("metadata_json") or "{}")
                meta["model"] = model
                message["metadata_json"] = json.dumps(meta, ensure_ascii=False)
        self._paint_timer.start(0)

    def _progress_text(self):
        text = f"{self._phase or '작업 준비 중'} · 경과 {self._duration(self._elapsed())}"
        if self._last_signal is not None:
            age = max(0, int(time.monotonic() - self._last_signal))
            text += f" · 마지막 CLI 응답 {age}초 전"
        elif self.runner.busy:
            text += " · CLI 응답 대기"
        if self._stream:
            text += f" · 답변 {len(self._stream):,}자 수신"
        if self._elapsed() >= 30 and not self._stream and self.runner.busy:
            text += "\n아직 답변이 도착하지 않았습니다. 기다리거나 중지 후 다시 요청할 수 있습니다. 최대 대기 4분."
        return text

    def _update_progress(self):
        if not self._busy:
            return
        self.status.setText(self._progress_text())
        if self._active_message is not None and not self._stream:
            self._paint_timer.start(0)

    def set_targets(self, codes):
        if self._busy:
            return
        self.targets.setText(", ".join(dict.fromkeys(str(code) for code in codes if code)))

    def _preset(self, kind):
        if self._busy:
            return
        if kind not in ("market", "compare") and not self.targets.text().strip():
            self.status.setText("분석할 종목을 먼저 선택해 주세요.")
            return
        if kind == "orders":
            self.logs.setChecked(True)
        if kind == "compare":
            self.attach.setChecked(True)
            self.logs.setChecked(False)
        self.question.setPlainText(QUESTIONS[kind])
        self._prepare(kind=kind)

    def _prepare(self, *, preview=False, kind=""):
        if self._busy or not self.store:
            return
        question = self.question.toPlainText().strip()
        if not preview and not question:
            self.status.setText("질문을 입력해 주세요.")
            return
        if len(question) > 12000:
            self.status.setText("질문은 12,000자 이내로 입력해 주세요.")
            return
        if self.logs.isChecked() and not self.targets.text().strip():
            self.status.setText("주문 기록을 첨부할 종목을 먼저 선택해 주세요.")
            return
        self._serial += 1
        serial = self._serial
        self._pending_request = {"question": redact(question), "preview": preview,
                                 "provider": self.provider.currentData(),
                                 "model": self._requested_model(), "kind": kind}
        self._set_busy(True)
        self._record_stage("첨부 자료 준비 중")
        kwargs = {"targets": self.targets.text().strip() if (self.attach.isChecked() or self.logs.isChecked()) else "",
                  "db_path": self.db_path,
                  "include_data": self.attach.isChecked(),
                  "include_logs": self.logs.isChecked(), "log_path": self.log_path,
                  "live_quotes": self.capture() if self.attach.isChecked() else {}}
        if kind == "compare":
            kwargs.update(targets="", include_data=True, include_logs=False, today_leaders=True)
            self.targets.clear()
            self.comparison_scope.setText("오늘 대장 후보를 새로 찾는 중입니다. 이전 분석 종목은 사용하지 않습니다.")
            self.comparison_scope.show()
            if self.market_scan:
                self._record_stage("키움 당일 상승률·거래대금 순위 조회 중")
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    kwargs["rankings"] = {"warnings": ["키움 순위 조회를 시작하지 못했습니다. 앱 화면과 당일 저장 자료만 사용합니다."]}
                else:
                    self._scan_task = loop.create_task(self._scan_and_prepare(serial, kwargs))
                    return
        self._start_context(serial, kwargs)

    async def _scan_and_prepare(self, serial, kwargs):
        try:
            try:
                kwargs["rankings"] = await asyncio.wait_for(self.market_scan(), timeout=30)
            except Exception as error:
                kwargs["rankings"] = {"warnings": ["키움 당일 순위 조회 실패: " + redact(str(error))]}
            if serial != self._serial or self._pending_request is None:
                return
            # 순위 조회가 끝난 시점의 화면 사본으로 비교 시차를 줄인다.
            kwargs["live_quotes"] = self.capture()
            self._record_stage("당일 대장 후보와 종목 근거 자료 준비 중")
            self._start_context(serial, kwargs)
        except asyncio.CancelledError:
            pass
        except Exception as error:
            self._prepared(serial, None, redact(str(error)))
        finally:
            if serial == self._serial:
                self._scan_task = None

    def _start_context(self, serial, kwargs):
        job = _ContextJob(serial, kwargs)
        self._jobs[serial] = job
        job.signals.ready.connect(self._prepared)
        QThreadPool.globalInstance().start(job)

    def _show_comparison_scope(self, context):
        comparison = context.get("leader_comparison")
        self.comparison_scope.setVisible(bool(comparison))
        if comparison:
            names = [s.get("stock_name") or s["stock_code"] for s in context.get("stocks", [])]
            summary = (f"오늘 대장 비교 · {comparison['trade_date']} · " + ", ".join(names)
                       + "\n" + comparison["scope"])
            if comparison.get("warnings"):
                summary += "\n" + " / ".join(comparison["warnings"])
            self.comparison_scope.setText(summary)

    def _prepared(self, serial, context, error):
        self._jobs.pop(serial, None)
        if serial != self._serial or self._pending_request is None:
            return
        request, self._pending_request = self._pending_request, None
        if error:
            if request["kind"] == "compare":
                self.comparison_scope.setText(error)
            self._record_stage("자료 준비 실패: " + error)
            self._set_busy(False)
            self.status.setText("자료 준비 실패: " + error)
            return
        self._context = context
        self._show_comparison_scope(context)
        comparison = context.get("leader_comparison")
        if comparison:
            names = [s.get("stock_name") or s["stock_code"] for s in context.get("stocks", [])]
            self.targets.setText(", ".join(s["stock_code"] for s in context.get("stocks", [])))
            request["question"] += "\n\n이번 비교 후보: " + ", ".join(names)
        if request["preview"]:
            self._set_busy(False)
            self._show_context(context)
            return
        try:
            self._record_stage(f"자료 준비 완료 · {len(context.get('stocks', []))}종목")
            if not self._session_id:
                self._session_id = self.store.create(request["provider"], request["model"])
            self.store.configure(self._session_id, request["provider"], request["model"])
            history = self.store.messages(self._session_id, include_context=False)
            self.store.add(self._session_id, "user", request["question"], context=context,
                           metadata={"task": request["kind"]})
            self._active_message = self.store.add(self._session_id, "assistant", "", status="running",
                metadata={"provider": request["provider"], "requested_model": request["model"] or "기본"})
            self._messages = self.store.messages(self._session_id, include_context=False)
            self._stream = ""
            self.question.clear()
            self._paint()
            self._refresh_sessions()
            self.runner.run(request["provider"], request["model"],
                            compose_prompt(request["question"], context, history))
        except Exception as exception:
            self._failed(redact(str(exception)))

    def _show_context(self, context):
        dialog = QDialog(self)
        dialog.setWindowTitle("AI에게 전달할 자료")
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        layout = QVBoxLayout(dialog)
        text = QPlainTextEdit()
        text.setReadOnly(True)
        text.setPlainText(json.dumps(context, ensure_ascii=False, indent=2))
        layout.addWidget(text)
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(dialog.reject)
        layout.addWidget(close)
        dialog.resize(760, 580)
        dialog.open()
        self.status.setText("자료 기준: " + context.get("captured_at", ""))

    def _text_changed(self, text):
        self._stream = text
        self._update_progress()
        if not self._paint_timer.isActive():
            self._paint_timer.start(75)

    def _paint(self):
        scroll = self.answer.verticalScrollBar()
        at_bottom = scroll.value() >= scroll.maximum() - 40
        old = scroll.value()
        self.answer.clear()
        dark = self.answer.palette().color(QPalette.ColorRole.Base).lightness() < 128
        link = "#83baff" if dark else "#175bb5"
        self.answer.document().setDefaultStyleSheet(
            f"a {{ color: {link}; }} th, td {{ padding: 5px 9px; }} "
            "table { border-collapse: collapse; }")
        palette = self.answer.palette()
        if palette.color(QPalette.ColorRole.Link) != QColor(link):
            palette.setColor(QPalette.ColorRole.Link, QColor(link))
            self.answer.setPalette(palette)
        cursor = self.answer.textCursor()
        heading = QTextCharFormat()
        heading.setFontWeight(QFont.Weight.Bold)
        for message in self._messages:
            label = "사용자" if message["role"] == "user" else "AI"
            if message["role"] == "assistant":
                meta = json.loads(message.get("metadata_json") or "{}")
                provider = meta.get("provider", "")
                model = meta.get("model") or meta.get("requested_model", "")
                label += (f" · {provider}" if provider else "") + (f" · {model}" if model else "")
            cursor.insertText(label + "\n", heading)
            cursor.setCharFormat(QTextCharFormat())
            content = self._stream if message["id"] == self._active_message else message["content"]
            if message["role"] == "assistant":
                if not content:
                    if message["id"] == self._active_message and self._busy:
                        content = self._progress_text() + "\n\n답변이 도착하면 여기에 표시됩니다."
                    elif message.get("status") == "interrupted":
                        content = "답변이 도착하기 전에 중지된 요청입니다. 다시 요청할 수 있습니다."
                    elif message.get("status") == "error":
                        content = "오류로 답변을 받지 못했습니다. 진행 기록을 확인해 주세요."
                    else:
                        content = "저장된 답변 내용이 없습니다."
                cursor.insertMarkdown(content,
                    QTextDocument.MarkdownFeature.MarkdownDialectGitHub
                    | QTextDocument.MarkdownFeature.MarkdownNoHTML)
            else:
                cursor.insertText(content)
            status = message.get("status")
            if status in ("interrupted", "error"):
                cursor.insertText("\n[중지됨]" if status == "interrupted" else "\n[오류]")
            cursor.insertBlock()
            cursor.insertBlock()
        if not self._messages:
            cursor.insertText("종목을 선택하고 분석 버튼을 누르거나 질문을 입력해 주세요.\n\n"
                              "앱에 저장된 뉴스·공시·일봉과 화면 시세를 함께 분석합니다.\n"
                              "첨부 자료 보기에서 실제로 보낼 내용을 확인할 수 있습니다.")
        # Qt의 Markdown 삽입기는 링크에 파랑을 직접 지정한다. 팔레트만
        # 바꾸면 다크 모드에서 어두운 파랑이 남으므로 링크 글자만 보정한다.
        anchors = []
        block = self.answer.document().begin()
        while block.isValid():
            iterator = block.begin()
            while not iterator.atEnd():
                fragment = iterator.fragment()
                if fragment.isValid() and fragment.charFormat().isAnchor():
                    anchors.append((fragment.position(), fragment.length()))
                iterator += 1
            block = block.next()
        color_format = QTextCharFormat()
        color_format.setForeground(QColor(link))
        for position, length in anchors:
            anchor = QTextCursor(self.answer.document())
            anchor.setPosition(position)
            anchor.setPosition(position + length, QTextCursor.MoveMode.KeepAnchor)
            anchor.mergeCharFormat(color_format)
        scroll.setValue(scroll.maximum() if at_bottom else old)

    def _completed(self, text, metadata):
        if metadata.get("probe"):
            self._record_stage(metadata.get("status", "연결 확인 완료"))
            self._set_busy(False)
            self.status.setText(metadata.get("status", "연결 확인 완료"))
            return
        status = metadata.get("status", "complete")
        self._record_stage("작업 중지됨" if status == "interrupted" else "분석 완료")
        saved = self._finish_message(text, status, metadata)
        actual_model = metadata.get("model") or metadata.get("requested_model") or "기본 모델"
        if saved:
            self.status.setText("작업 중지됨" if status == "interrupted" else
                                f"분석 완료 · {actual_model} · 소요 {self._duration(self._elapsed())} · 자료 {self._context.get('captured_at', '')}")

    def _finish_message(self, text, status, metadata):
        self._paint_timer.stop()
        saved_meta = next((json.loads(m.get("metadata_json") or "{}")
                           for m in self._messages if m["id"] == self._active_message), {})
        metadata = {**saved_meta, **self.runner.diagnostics(), **metadata,
                    "progress_log": list(self._progress_log),
                    "ui_elapsed_seconds": round(self._elapsed(), 1)}
        saved = True
        try:
            if self._active_message is not None:
                self.store.finish(self._active_message, text, status, metadata)
                self._messages = self.store.messages(self._session_id, include_context=False)
        except Exception as error:
            saved = False
            self.status.setText("답변 저장 실패: " + str(error))
            # 저장 실패여도 받은 답변은 화면에 남긴다.
            for message in self._messages:
                if message["id"] == self._active_message:
                    message.update(content=text, status=status)
        self._active_message = None
        self._stream = ""
        self._set_busy(False)
        self._paint()
        self._refresh_sessions()
        return saved

    def _failed(self, error):
        self._record_stage("오류: " + redact(error))
        text = self._stream + ("\n\n" if self._stream else "") + "오류: " + redact(error)
        self._finish_message(text, "error", {})
        self.status.setText("AI 작업 실패: " + redact(error))

    def _stop(self):
        self._serial += 1
        self._pending_request = None
        if self._scan_task:
            self._scan_task.cancel()
            self._scan_task = None
        if self.runner.busy:
            self.runner.stop()
        else:
            self._record_stage("자료 준비 중지됨")
            self._set_busy(False)
            self.status.setText("자료 준비를 중지했습니다.")

    def _probe(self):
        if self._busy:
            return
        self._set_busy(True)
        try:
            self.runner.probe(self.provider.currentData())
        except Exception as error:
            self._failed(str(error))

    def _models_ready(self, models):
        provider = self.provider.currentData()
        self._models[provider] = models
        self._fill_models(self._requested_model())

    def _fill_models(self, selected=""):
        self.model.clear()
        self.model.addItem("기본 모델", "")
        for model in self._models[self.provider.currentData()]:
            if model.get("id"):
                self.model.addItem(str(model["label"]), str(model["id"]))
        index = self.model.findData(selected)
        if index >= 0:
            self.model.setCurrentIndex(index)
        elif selected:
            self.model.setEditText(selected)

    def _provider_changed(self):
        self._fill_models()
        if not self._loading:
            self._new_chat()

    def _new_chat(self):
        if self._busy:
            return
        self._session_id = ""
        self._messages = []
        self._context = {}
        self.comparison_scope.hide()
        self._stream = ""
        self._progress_log = []
        self.details.clear()
        self.retry_button.setEnabled(False)
        self.history.blockSignals(True)
        self.history.setCurrentRow(-1)
        self.history.blockSignals(False)
        self.status.setText("새 대화 · 질문을 보내면 저장됩니다.")
        self._paint()

    def _refresh_sessions(self):
        if not self.store:
            return
        self.history.blockSignals(True)
        self.history.clear()
        try:
            for row in self.store.conversations():
                item = QListWidgetItem(f"{row['title']}\n{row['provider']} · {row['updated_at'][5:16]}")
                item.setData(Qt.ItemDataRole.UserRole, row)
                self.history.addItem(item)
                if row["id"] == self._session_id:
                    self.history.setCurrentItem(item)
        except Exception as error:
            self.status.setText("대화 목록을 읽지 못했습니다: " + str(error))
        finally:
            self.history.blockSignals(False)

    def _load_session(self, item, previous=None):
        if item is None or self._busy or not self.store:
            return
        row = item.data(Qt.ItemDataRole.UserRole)
        self._loading = True
        try:
            self._session_id = row["id"]
            self.provider.setCurrentIndex(max(0, self.provider.findData(row["provider"])))
            self._fill_models(row["model"])
            self._messages = self.store.messages(self._session_id, include_context=False)
            self._stream = ""
            self._context = self.store.last_context(self._session_id)
            self._show_comparison_scope(self._context)
            self.set_targets([r["stock_code"] for r in self._context.get("stocks", [])])
            self._progress_log = []
            for message in reversed(self._messages):
                if message["role"] == "assistant":
                    meta = json.loads(message.get("metadata_json") or "{}")
                    self._progress_log = list(meta.get("progress_log", []))
                    break
            self.details.setPlainText("\n".join(self._progress_log))
            self.retry_button.setEnabled(any(m["role"] == "user" for m in self._messages))
            self._paint()
            self.status.setText("저장된 대화 · 다음 질문에는 새로 준비한 자료를 첨부합니다.")
        except Exception as error:
            self.status.setText("대화를 읽지 못했습니다: " + str(error))
        finally:
            self._loading = False

    def _retry_last(self):
        if self._busy:
            return
        message = next((m for m in reversed(self._messages) if m["role"] == "user"), None)
        if message:
            kind = json.loads(message.get("metadata_json") or "{}").get("task", "")
            if kind == "compare":
                self._preset(kind)
                return
            self.question.setPlainText(message["content"])
            self._prepare(kind=kind)

    def _delete_session(self):
        if self._busy or not self._session_id or not self.store:
            return
        try:
            self.store.delete(self._session_id)
            self._new_chat()
            self._refresh_sessions()
        except Exception as error:
            self.status.setText("대화를 지우지 못했습니다: " + str(error))

    def _choose_watchlist(self):
        try:
            rows = read_catalog(self.db_path, watch=True)
        except Exception as error:
            self.status.setText("관심목록을 읽지 못했습니다: " + str(error))
            return
        if not rows:
            self.status.setText("종토방 관심목록이 비어 있습니다.")
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("분석할 관심종목 선택")
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        layout = QVBoxLayout(dialog)
        notice = QLabel(f"Ctrl 또는 Shift로 선택 · 최대 {MAX_STOCKS}종목")
        layout.addWidget(notice)
        choices = QListWidget()
        choices.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        for row in rows:
            item = QListWidgetItem(f"{row['stock_name']} ({row['stock_code']})")
            item.setData(Qt.ItemDataRole.UserRole, row["stock_code"])
            choices.addItem(item)
        layout.addWidget(choices)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        def accept():
            codes = [item.data(Qt.ItemDataRole.UserRole) for item in choices.selectedItems()]
            if not 1 <= len(codes) <= MAX_STOCKS:
                notice.setText(f"1~{MAX_STOCKS}종목을 선택해 주세요.")
                return
            self.set_targets(codes)
            dialog.accept()
        buttons.accepted.connect(accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.resize(400, 470)
        dialog.open()

    def shutdown(self):
        self._serial += 1
        self._pending_request = None
        if self._scan_task:
            self._scan_task.cancel()
            self._scan_task = None
        self.runner.shutdown()


class AiAssistantTabMixin:
    def _build_ai_page(self, layout):
        self._ai_page = AiAssistantWidget(self, capture=self._ai_capture_quotes,
                                         selection=self._ai_selected_codes,
                                         market_scan=self._ai_scan_market)
        layout.addWidget(self._ai_page)

    async def _ai_scan_market(self):
        rest = getattr(self, "_rest", None) or getattr(getattr(self, "_app", None), "rest", None)
        if rest is None:
            raise RuntimeError("키움 연결이 없습니다.")
        # 기존 앱의 인증과 조회 제한 관리를 공유한다. 주문/웹소켓 연결은 만들지 않는다.
        result = {"warnings": ["순위 응답에는 거래일자·틱 수신 시각이 없어 조회 시각만 표시합니다. 휴장·장 시작 전에는 전일 자료일 수 있습니다."],
                  "started_at": datetime.now().astimezone().isoformat(timespec="seconds")}
        for key, call in (("change_rate", lambda: rest.change_rate_rank("1")),
                          ("trading_value", lambda: rest.trade_value_rank("1"))):
            try:
                result[key] = await call()
            except Exception as error:
                result[key] = []
                result["warnings"].append(key + " 조회 실패: " + redact(str(error)))
        result["finished_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        return result

    def _ai_selected_codes(self):
        title = self._tabs.tabText(self._tabs.currentIndex())
        if title == "AI 분석":
            title = getattr(self, "_ai_source_tab", "종목뉴스·종토방")
        codes = []
        if title == "종목뉴스·종토방":
            table, column, role = self._news_watch_table, 0, Qt.ItemDataRole.UserRole + 2
        elif title == "상한가":
            table, column, role = self._limit_table, 0, Qt.ItemDataRole.UserRole + 2
        elif title == "실시간 뉴스":
            table, column, role = self._ls_news_table, 2, Qt.ItemDataRole.UserRole
        elif title == "텔레그램 뉴스":
            table, column, role = self._telegram_table, 3, Qt.ItemDataRole.UserRole
        elif title == "테마":
            table = self._theme_table
            for row in sorted({index.row() for index in table.selectedIndexes()}):
                item = table.item(row, 5)
                if item:
                    codes.extend(part.strip().split(" ")[0] for part in item.text().split(","))
            return list(dict.fromkeys(code for code in codes if valid_code(code)))
        else:
            return []
        for row in sorted({index.row() for index in table.selectedIndexes()}):
            item = table.item(row, column)
            value = item.data(role) if item else None
            codes.extend(value if isinstance(value, (list, tuple)) else [value])
        if not codes and title == "종목뉴스·종토방":
            codes.append(getattr(self, "_selected_watch_code", ""))
        return list(dict.fromkeys(str(code) for code in codes if code and valid_code(str(code))))

    def _open_ai_analysis(self):
        codes = self._ai_selected_codes()
        if codes:
            self._ai_page.set_targets(codes)
        self._select_tab("AI 분석")
        self._ai_page.question.setFocus()

    def _ai_capture_quotes(self):
        result = {}
        fields = ("name", "price", "rate", "vol", "acc_value", "open", "high", "low",
                  "upper", "bid_price", "bid_qty", "ask_price", "ask_qty", "exp_price",
                  "exp_qty", "trade_time", "trade_ex")
        stamp = datetime.now().astimezone().isoformat(timespec="seconds")
        for view in getattr(getattr(self, "_app", None), "views", []):
            model = view.screen.model
            for code, row in model.rows.items():
                if code in result or not row.get("price"):
                    continue
                result[code] = {"copied_at": stamp, "quote_received_at": "기록 없음",
                                "display_market": "KRX+NXT" if model.unified else "KRX",
                                "values": {key: row[key] for key in fields if key in row}}
                cnt, yclose = getattr(model, "limit_cnt", {}).get(code, (0, 0))
                today_limit = (row.get("upper", 0) > 0 and row.get("price") == row.get("upper")
                               and row.get("upper") != yclose)
                result[code]["consecutive_days"] = cnt + int(today_limit)
        return result
