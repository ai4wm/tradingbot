# -*- coding: utf-8 -*-
"""설치된 CLI와 파이프로 통신한다. 터미널/화면 클릭이나 주문 호출 없음."""
from __future__ import annotations

import codecs
import json
import os
import shutil
import time
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, QTimer, Signal

from ai_context import ROOT, SYSTEM_INSTRUCTIONS, redact


def cli_command(provider: str) -> list[str]:
    if provider not in ("codex", "claude"):
        raise ValueError("지원하지 않는 AI입니다.")
    if os.name == "nt":
        # npm의 .cmd는 쉘과 별도 자식을 만든다. 실제 실행 파일을 우선 찾아
        # 질문을 쉘 문자열에 넣지 않고, 중지 때도 실행한 프로세스를 종료한다.
        candidate = shutil.which(provider + ".exe")
        if candidate:
            return [candidate]
        roots = []
        found = shutil.which(provider)
        if found:
            roots.append(Path(found).parent)
        if os.environ.get("APPDATA"):
            roots.append(Path(os.environ["APPDATA"]) / "npm")
        for root in dict.fromkeys(roots):
            if provider == "claude":
                path = root / "node_modules/@anthropic-ai/claude-code/bin/claude.exe"
                if path.is_file():
                    return [str(path)]
            else:
                package = root / "node_modules/@openai/codex"
                for pattern in (
                    "node_modules/@openai/codex-win32-*/vendor/*/bin/codex.exe",
                    "node_modules/@openai/codex-win32-*/vendor/*/codex/codex.exe",
                ):
                    paths = sorted(package.glob(pattern))
                    if paths:
                        return [str(paths[0])]
        raise FileNotFoundError(f"{provider} 실행 파일을 찾지 못했습니다. CLI 설치를 확인해 주세요.")
    found = shutil.which(provider)
    if not found:
        raise FileNotFoundError(f"{provider} CLI가 설치되어 있지 않습니다.")
    return [found]


class CliRunner(QObject):
    text_changed = Signal(str)
    status_changed = Signal(str)
    completed = Signal(str, dict)
    failed = Signal(str)
    models_ready = Signal(list)
    busy_changed = Signal(bool)
    signal_received = Signal()
    model_changed = Signal(str)

    def __init__(self, parent=None, *, workspace: Path | None = None,
                 command_resolver=cli_command):
        super().__init__(parent)
        self.workspace = Path(workspace or ROOT / "data" / "ai_workspace")
        self._resolve = command_resolver
        self._proc = None
        self._processes = set()
        self._busy = False
        self._deadline = QTimer(self)
        self._deadline.setSingleShot(True)
        self._deadline.timeout.connect(self._timeout)
        self._ready_deadline = QTimer(self)
        self._ready_deadline.setSingleShot(True)
        self._ready_deadline.timeout.connect(self._ready_timeout)

    @property
    def busy(self):
        return self._busy

    def probe(self, provider: str):
        self._launch(provider, "", "", probe=True)

    def run(self, provider: str, model: str, prompt: str):
        self._launch(provider, model.strip(), prompt, probe=False)

    def _launch(self, provider, model, prompt, *, probe):
        if self._busy:
            raise RuntimeError("진행 중인 AI 작업을 먼저 중지해 주세요.")
        self._provider, self._model, self._prompt = provider, model, prompt
        self._probe, self._done = probe, False
        self._pending = {}
        self._request_id = 0
        self._buffer, self._stderr = "", ""
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._err_decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._parts, self._part_order = {}, []
        self._thread_id, self._turn_id = "", ""
        self._metadata = {"provider": provider, "requested_model": model or "기본"}
        self._started_at = time.monotonic()
        self._last_event_at = None
        self._elapsed_final = None
        self._phase_name = ""
        self._phase_log = []
        try:
            command = self._resolve(provider)
            self.workspace.mkdir(parents=True, exist_ok=True)
        except (OSError, ValueError) as error:
            self.failed.emit(str(error))
            return
        process = QProcess(self)
        self._processes.add(process)
        self._proc = process
        process.setWorkingDirectory(str(self.workspace.resolve()))
        environment = QProcessEnvironment.systemEnvironment()
        environment.insert("NO_COLOR", "1")
        process.setProcessEnvironment(environment)
        process.setProcessChannelMode(QProcess.ProcessChannelMode.SeparateChannels)
        process.readyReadStandardOutput.connect(lambda: self._read_stdout(process))
        process.readyReadStandardError.connect(lambda: self._read_stderr(process))
        process.started.connect(lambda: self._started(process))
        process.errorOccurred.connect(lambda error: self._process_error(process, error))
        process.finished.connect(lambda code, status: self._exited(process, code, status))
        if provider == "codex":
            args = ["app-server", "--listen", "stdio://",
                    "-c", "features.shell_tool=false", "-c", "features.unified_exec=false",
                    "-c", "mcp_servers={}", "-c", 'web_search="disabled"']
        elif probe:
            args = ["--version"]
        else:
            args = ["-p", "--input-format", "text", "--output-format", "stream-json",
                    "--verbose", "--include-partial-messages", "--no-session-persistence",
                    "--tools", "", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                    "--setting-sources", "", "--permission-mode", "default",
                    "--system-prompt", SYSTEM_INSTRUCTIONS]
            if model:
                args += ["--model", model]
        process.setProgram(command[0])
        process.setArguments(command[1:] + args)
        self._busy = True
        self.busy_changed.emit(True)
        self._phase("CLI 실행 중")
        self._deadline.start(20_000 if probe else 240_000)
        if not probe:
            self._ready_deadline.start(45_000)
        process.start()

    def _phase(self, text):
        if text == self._phase_name:
            return
        self._phase_name = redact(text)
        self._phase_log.append({"at": datetime.now().astimezone().isoformat(timespec="seconds"),
                                "elapsed": round(time.monotonic() - self._started_at, 1),
                                "stage": self._phase_name})
        self._phase_log = self._phase_log[-50:]
        self.status_changed.emit(self._phase_name)

    def _note_model(self, model):
        if model and model != self._metadata.get("model"):
            self._metadata["model"] = str(model)
            self.model_changed.emit(str(model))

    def diagnostics(self):
        started = getattr(self, "_started_at", None)
        elapsed = (getattr(self, "_elapsed_final", None)
                   if getattr(self, "_elapsed_final", None) is not None
                   else time.monotonic() - started if started is not None else 0)
        return {"elapsed_seconds": round(elapsed, 1),
                "phase_log": list(getattr(self, "_phase_log", [])),
                "last_phase": getattr(self, "_phase_name", ""),
                "model": getattr(self, "_metadata", {}).get("model", "")}

    def _started(self, process):
        if process is not self._proc or self._done:
            return
        if self._provider == "codex":
            self._phase("Codex 시작됨 · 연결 확인 중")
            self._rpc("initialize", {"clientInfo": {
                "name": "trading_bot_analysis", "title": "주식 앱 AI 분석", "version": "1.0"}},
                self._initialized)
        elif not self._probe:
            process.write(self._prompt.encode("utf-8"))
            process.closeWriteChannel()
            self._phase("Claude 시작됨 · 질문 전송 완료 · 응답 대기")

    def _rpc(self, method, params, callback=None):
        self._request_id += 1
        request = {"id": self._request_id, "method": method, "params": params}
        if callback:
            self._pending[self._request_id] = callback
        self._send(request)

    def _send(self, message):
        if self._proc and not self._done:
            self._proc.write((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))

    def _initialized(self, result):
        self._send({"method": "initialized", "params": {}})
        self._phase("Codex 연결 확인 완료")
        if self._probe:
            self._rpc("account/read", {}, self._account)
        else:
            self._phase("Codex 대화 준비 중")
            params = {"cwd": str(self.workspace.resolve()), "sandbox": "read-only",
                      "approvalPolicy": "never", "baseInstructions": SYSTEM_INSTRUCTIONS,
                      "ephemeral": True}
            if self._model:
                params["model"] = self._model
            self._rpc("thread/start", params, self._thread_started)

    def _account(self, result):
        # 이메일/토큰을 화면이나 대화 DB에 기록하지 않는다.
        if result.get("requiresOpenaiAuth", True) and not result.get("account"):
            self._fail("Codex 로그인이 필요합니다. 터미널에서 codex login을 실행해 주세요.")
            return
        self._rpc("model/list", {"limit": 100}, self._models)

    def _models(self, result):
        models = [{"id": row.get("model") or row.get("id"),
                   "label": row.get("displayName") or row.get("model") or row.get("id")}
                  for row in result.get("data", []) if not row.get("hidden")]
        self.models_ready.emit(models)
        self._succeed("", {"probe": True, "status": "로그인 및 모델 목록 확인 완료"})

    def _thread_started(self, result):
        self._thread_id = result["thread"]["id"]
        self._note_model(result.get("model", self._model))
        params = {"threadId": self._thread_id,
                  "input": [{"type": "text", "text": self._prompt, "text_elements": []}]}
        if self._model:
            params["model"] = self._model
        self._phase("Codex 대화 준비 완료 · 분석 요청 중")
        self._rpc("turn/start", params, self._turn_started)

    def _turn_started(self, result):
        self._turn_id = result.get("turn", {}).get("id", "")
        self._ready_deadline.stop()
        if self._phase_name == "Codex 대화 준비 완료 · 분석 요청 중":
            self._phase("Codex 요청 접수 · 답변 생성 대기")

    def _read_stdout(self, process):
        data = bytes(process.readAllStandardOutput())
        if process is not self._proc or self._done:
            return
        self._buffer += self._decoder.decode(data)
        if len(self._buffer) > 4_000_000:
            self._fail("AI 응답 한 줄이 너무 큽니다.")
            return
        while "\n" in self._buffer and not self._done:
            line, self._buffer = self._buffer.split("\n", 1)
            self._line(line.strip())

    def _line(self, line):
        if not line or (self._provider == "claude" and self._probe):
            return
        try:
            event = json.loads(line)
        except ValueError:
            # 초기 배너는 stderr와 같은 진단으로 보관한다. 답변으로 섞지 않는다.
            self._stderr = (self._stderr + "\n" + redact(line))[-4000:]
            return
        if not isinstance(event, dict):
            return
        self._last_event_at = time.monotonic()
        self.signal_received.emit()
        try:
            if self._provider == "codex":
                self._codex_event(event)
            else:
                self._claude_event(event)
        except (KeyError, TypeError, AttributeError, ValueError) as error:
            self._fail("CLI 응답 형식을 확인하지 못했습니다: " + str(error))

    def _codex_event(self, event):
        if "id" in event and "method" not in event:
            callback = self._pending.pop(event["id"], None)
            if event.get("error"):
                error = event["error"]
                self._fail(str(error.get("message", error)))
            elif callback:
                callback(event.get("result") or {})
            return
        if "id" in event:
            # 앱 분석 세션에 승인/파일 접근 등 실행 권한을 주지 않는다.
            self._send({"id": event["id"], "error": {
                "code": -32601, "message": "Analysis-only client: operation is unavailable"}})
            return
        method, params = event.get("method"), event.get("params") or {}
        if method == "turn/started":
            self._turn_id = params.get("turn", {}).get("id", "")
            self._ready_deadline.stop()
            if self._phase_name == "Codex 대화 준비 완료 · 분석 요청 중":
                self._phase("Codex 요청 접수 · 답변 생성 대기")
        elif method == "item/started":
            item = params.get("item") or {}
            if item.get("type") == "reasoning":
                self._phase("Codex 답변 생성 중")
        elif method in ("item/reasoning/summaryTextDelta", "item/reasoning/textDelta"):
            # 비공개 추론 내용은 표시·저장하지 않고 이벤트 수신 상태만 알린다.
            self._phase("Codex 답변 생성 중")
        elif method == "error":
            error = params.get("error") or {}
            message = str(error.get("message") or "AI 연결 오류")
            if params.get("willRetry"):
                self._phase("연결 오류 · CLI가 재시도 중: " + message[:350])
            else:
                self._fail(message)
        elif method == "item/agentMessage/delta":
            self._append(params.get("itemId", "answer"), str(params.get("delta", "")))
        elif method == "item/completed":
            item = params.get("item") or {}
            if item.get("type") == "agentMessage":
                self._append(item.get("id", "answer"), item.get("text", ""), replace=True)
        elif method == "thread/tokenUsage/updated":
            self._metadata["usage"] = params.get("tokenUsage", {})
        elif method == "turn/completed":
            turn = params.get("turn") or {}
            if turn.get("status") == "completed":
                self._succeed(self.text, self._metadata)
            else:
                error = turn.get("error") or {}
                self._fail(str(error.get("message") or turn.get("status") or "분석 실패"))

    def _claude_event(self, event):
        kind = event.get("type")
        if kind == "system" and event.get("subtype") == "init":
            self._note_model(event.get("model", self._model))
            self._ready_deadline.stop()
            self._phase("Claude 준비 완료 · 답변 생성 대기")
        elif kind == "stream_event":
            data = event.get("event") or {}
            if data.get("type") == "message_start":
                self._ready_deadline.stop()
                self._note_model((data.get("message") or {}).get("model"))
                self._phase("Claude 요청 접수 · 답변 생성 대기")
            elif data.get("type") == "content_block_start":
                if (data.get("content_block") or {}).get("type") == "thinking":
                    self._phase("Claude 답변 생성 중")
            elif data.get("type") == "content_block_delta":
                delta = data.get("delta") or {}
                if delta.get("type") == "text_delta":
                    self._append("answer", str(delta.get("text", "")))
                elif delta.get("type") == "thinking_delta":
                    self._phase("Claude 답변 생성 중")
        elif kind == "assistant":
            parts = [block.get("text", "") for block in
                     (event.get("message") or {}).get("content", [])
                     if block.get("type") == "text"]
            if parts:
                self._append("answer", "\n".join(parts), replace=True)
        elif kind == "result":
            if event.get("is_error"):
                errors = event.get("errors") or [event.get("result") or "Claude 분석 실패"]
                self._fail("\n".join(map(str, errors)))
                return
            result = event.get("result") or self.text
            metadata = dict(self._metadata)
            for key in ("usage", "total_cost_usd", "duration_ms", "modelUsage"):
                if key in event:
                    metadata[key] = event[key]
            self._succeed(str(result), metadata)

    @property
    def text(self):
        return "\n\n".join(self._parts[key] for key in self._part_order)

    def _append(self, key, value, replace=False):
        if value:
            self._ready_deadline.stop()
            self._phase("답변 수신 중")
        if key not in self._parts:
            self._parts[key] = ""
            self._part_order.append(key)
        self._parts[key] = redact(str(value)) if replace else self._parts[key] + str(value)
        if sum(map(len, self._parts.values())) > 300_000:
            self._fail("AI 답변이 너무 길어 작업을 중지했습니다.")
            return
        self.text_changed.emit(redact(self.text))

    def _read_stderr(self, process):
        data = bytes(process.readAllStandardError())
        if process is self._proc and not self._done:
            self._stderr = (self._stderr + self._err_decoder.decode(data))[-4000:]

    def _process_error(self, process, error):
        if process is self._proc and not self._done and error == QProcess.ProcessError.FailedToStart:
            self._fail("CLI를 실행하지 못했습니다: " + process.errorString())
            self._processes.discard(process)
            process.deleteLater()

    def _exited(self, process, code, status):
        if process is self._proc and not self._done:
            self._read_stdout(process)
            self._read_stderr(process)
            self._buffer += self._decoder.decode(b"", final=True)
            if self._buffer.strip() and not self._done:
                self._line(self._buffer.strip())
            if not self._done:
                if self._probe and self._provider == "claude" and code == 0:
                    self.models_ready.emit([
                        {"id": "sonnet", "label": "sonnet (별칭)"},
                        {"id": "opus", "label": "opus (별칭)"}])
                    self._succeed("", {"probe": True, "status":
                        "Claude 설치 확인 · 로그인과 모델 권한은 첫 질문에서 확인합니다."})
                else:
                    self._fail(f"CLI가 결과 없이 종료됐습니다 ({code}).\n" + self._stderr[-1800:])
        self._processes.discard(process)
        try:
            process.deleteLater()
        except RuntimeError:
            pass

    def _finish(self):
        self._done = True
        self._elapsed_final = time.monotonic() - self._started_at
        self._deadline.stop()
        self._ready_deadline.stop()
        self._busy = False
        self._prompt = ""
        process, self._proc = self._proc, None
        if process and process.state() != QProcess.ProcessState.NotRunning:
            process.terminate()
            QTimer.singleShot(800, lambda p=process: self._kill_if_running(p))
        self.busy_changed.emit(False)

    @staticmethod
    def _kill_if_running(process):
        try:
            if process.state() != QProcess.ProcessState.NotRunning:
                process.kill()
        except RuntimeError:  # 이미 finished에서 Qt 객체를 지웠다.
            pass

    def _succeed(self, text, metadata):
        if self._done:
            return
        if not metadata.get("probe") and not text.strip():
            self._fail("AI가 빈 답변을 반환했습니다.")
            return
        details = {**self._metadata, **metadata, **self.diagnostics()}
        self._finish()
        self.completed.emit(redact(text), details)

    def _fail(self, message):
        if self._done:
            return
        self._finish()
        self.failed.emit(redact(message))

    def _timeout(self):
        self._fail("최대 대기 시간을 초과했습니다. 마지막 단계: "
                   + self._phase_name + ". 연결을 확인한 뒤 다시 요청해 주세요.")

    def _ready_timeout(self):
        self._fail("AI 연결 준비가 45초 안에 끝나지 않았습니다. 마지막 단계: "
                   + self._phase_name + ". 연결을 확인한 뒤 다시 요청해 주세요.")

    def stop(self):
        if not self._busy:
            return
        if self._provider == "codex" and self._thread_id and self._turn_id:
            self._rpc("turn/interrupt", {"threadId": self._thread_id, "turnId": self._turn_id})
        text = redact(self.text)
        details = {**self._metadata, **self.diagnostics(), "status": "interrupted"}
        self._finish()
        self.completed.emit(text, details)

    def shutdown(self):
        self.stop()
        # 완료 응답을 받은 뒤 종료 중인 프로세스도 남아 있을 수 있다.
        # 새 요청이 시작됐거나 창을 닫아도 이전 프로세스를 놓치지 않는다.
        for process in list(self._processes):
            self._kill_if_running(process)
            try:
                process.waitForFinished(500)
            except RuntimeError:
                pass
