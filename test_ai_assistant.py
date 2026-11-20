# -*- coding: utf-8 -*-
"""AI 연결의 파이프 통신·중지·자료 경계·대화 복원을 임시 자료로 검사한다.

기본 실행은 가짜 CLI만 사용한다. --live-probe는 실제 설치와 로그인만 확인,
--live-smoke는 두 CLI에 자료 없는 짧은 연결 시험 질문을 보낸다.
"""
import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEventLoop, Qt, QTimer
from PySide6.QtGui import QFont, QFontDatabase, QPalette
from PySide6.QtWidgets import QApplication, QMainWindow, QTableWidget, QTableWidgetItem, QTabWidget

from ai_cli_runner import CliRunner
from ai_context import build_context, compose_prompt, read_catalog, tail_audit
from ai_sessions import SessionStore
from analysis_db import initialize
from ui.ai_assistant_tab import AiAssistantTabMixin, AiAssistantWidget, AnswerBrowser


APP = QApplication.instance() or QApplication([])
if Path("C:/Windows/Fonts/gulim.ttc").exists():
    QFontDatabase.addApplicationFont("C:/Windows/Fonts/gulim.ttc")
    APP.setFont(QFont("GulimChe", 10))


FAKE_CLI = r'''
import json,sys,time
def out(value, split=False):
    data=(json.dumps(value,ensure_ascii=False)+'\n').encode('utf-8')
    if split:
        for n in range(0,len(data),2):
            sys.stdout.buffer.write(data[n:n+2]);sys.stdout.buffer.flush()
    else:
        sys.stdout.buffer.write(data);sys.stdout.buffer.flush()
if 'app-server' in sys.argv:
    for line in sys.stdin:
        r=json.loads(line);m=r.get('method');i=r.get('id');p=r.get('params') or {}
        if m=='initialize':
            if '--stall-initialize' in sys.argv:continue
            out({'id':i,'result':{}})
        elif m=='account/read':out({'id':i,'result':{'requiresOpenaiAuth':True,'account':{'email':'private@example.test'}}})
        elif m=='model/list':out({'id':i,'result':{'data':[{'model':'test-model','displayName':'Test model'}]}})
        elif m=='thread/start':out({'id':i,'result':{'thread':{'id':'t1'},'model':p.get('model','test-model')}})
        elif m=='turn/start':
            text=p['input'][0]['text']
            if 'HOLD' in text:
                out({'method':'turn/started','params':{'turn':{'id':'u1'}}})
                out({'method':'item/started','params':{'item':{'id':'r','type':'reasoning'}}})
                out({'method':'item/reasoning/summaryTextDelta','params':{'delta':'PRIVATE_NEVER_SHOW'}})
                out({'id':i,'result':{'turn':{'id':'u1'}}})
                continue
            out({'id':i,'result':{'turn':{'id':'u1'}}})
            if 'FATAL_NOTICE' in text:
                out({'method':'error','params':{'error':{'message':'사용량 한도 오류'},'willRetry':False}})
                continue
            if 'RETRY_NOTICE' in text:
                out({'method':'error','params':{'error':{'message':'연결 지연 api_key=sk-abcdefghijklmnopqrstuvwxyz123456'},'willRetry':True}})
            if 'SLOW' in text:
                out({'method':'item/agentMessage/delta','params':{'itemId':'a','delta':'진행 중'}})
                continue
            out({'method':'item/agentMessage/delta','params':{'itemId':'a','delta':'연결 '}},True)
            out({'method':'item/agentMessage/delta','params':{'itemId':'a','delta':'성공'}},True)
            out({'method':'item/completed','params':{'item':{'type':'agentMessage','id':'a','text':'연결 성공'}}})
            out({'method':'thread/tokenUsage/updated','params':{'tokenUsage':{'total':{'totalTokens':5}}}})
            out({'method':'turn/completed','params':{'turn':{'id':'u1','status':'completed'}}})
        elif m=='turn/interrupt':out({'id':i,'result':{}})
elif '--version' in sys.argv:print('Claude test CLI 1.0')
else:
    prompt=sys.stdin.read()
    if 'FAIL' in prompt:
        print('api_key=sk-abcdefghijklmnopqrstuvwxyz123456',file=sys.stderr);sys.exit(7)
    out({'type':'system','subtype':'init','model':'test-claude'})
    out({'type':'stream_event','event':{'type':'content_block_delta','delta':{'type':'thinking_delta','thinking':'PRIVATE_NEVER_SHOW'}}})
    for text in ['연결 ','성공']:
        out({'type':'stream_event','event':{'type':'content_block_delta','delta':{'type':'text_delta','text':text}}},True)
    out({'type':'assistant','message':{'content':[{'type':'text','text':'연결 성공'}]}})
    out({'type':'result','is_error':False,'result':'연결 성공','total_cost_usd':0.001,'usage':{'input_tokens':3}})
'''


def wait_until(predicate, timeout=6):
    deadline = time.monotonic() + timeout
    while not predicate():
        APP.processEvents()
        if time.monotonic() > deadline:
            raise AssertionError("Qt operation timed out")
        time.sleep(0.005)
    APP.processEvents()


def seed(path):
    initialize(path)
    with closing(sqlite3.connect(path)) as db, db:
        for code, name in (("005930", "시험전자"), ("0010S0", "시험신규")):
            db.execute("INSERT INTO stocks(stock_code,stock_name,market,updated_at) VALUES(?,?,?,?)",
                       (code, name, "KOSPI", "2026-11-20T09:00:00+09:00"))
            db.execute("INSERT INTO realtime_watchlist(stock_code,added_at,updated_at) VALUES(?,?,?)",
                       (code, "2026-11-20", "2026-11-20"))
        db.execute("INSERT INTO daily_prices(trade_date,stock_code,close_price,volume,source,collected_at) "
                   "VALUES('2026-11-19','005930',1000,123,'KRX','2026-11-19T16:00:00+09:00')")
        source_id = db.execute("SELECT source_id FROM content_sources LIMIT 1").fetchone()[0]
        db.execute("INSERT INTO news_items(source_id,source_item_key,first_collected_at,last_seen_at,"
                   "published_at_source,current_title,current_summary,original_url) VALUES(?,?,?,?,?,?,?,?)",
                   (source_id, "news1", "2026-11-20", "2026-11-20", "2026-11-20T09:01:00+09:00",
                    '시험전자 계약 상담', 'password=abc123 자료에 나온 \\"표현\\"', 'https://example.test/news'))
        news_id = db.execute("SELECT news_id FROM news_items").fetchone()[0]
        db.execute("INSERT INTO news_stock_maps(news_id,stock_code,match_method,matched_text,mapped_at) "
                   "VALUES(?,'005930','STOCK_PAGE','시험전자','2026-11-20')", (news_id,))


class AiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="trading_ai_test_")
        self.root = Path(self.temp.name)
        self.db = self.root / "market.db"
        seed(self.db)
        self.cli = self.root / "fake_cli.py"
        self.cli.write_text(FAKE_CLI, encoding="utf-8")
        self.runners = []
        self.widgets = []

    def runner(self):
        runner = CliRunner(workspace=self.root / "workspace",
                           command_resolver=lambda provider: [sys.executable, str(self.cli)])
        self.runners.append(runner)
        return runner

    def tearDown(self):
        for widget in self.widgets:
            widget.shutdown()
            widget.close()
        for runner in self.runners:
            runner.shutdown()
        wait_until(lambda: not any(r.busy for r in self.runners))
        APP.processEvents()
        self.assertEqual(self.root.resolve().parent, Path(tempfile.gettempdir()).resolve())
        self.assertTrue(self.root.name.startswith("trading_ai_test_"))
        self.temp.cleanup()

    def test_readonly_context_and_sources(self):
        before = self.db.read_bytes()
        context = build_context("시험전자,0010S0", db_path=self.db,
            live_quotes={"005930": {"copied_at": "2026-11-20T10:00:00+09:00",
                                    "quote_received_at": "기록 없음", "values": {"price": 1100}}})
        self.assertEqual(before, self.db.read_bytes())
        self.assertEqual([s["stock_code"] for s in context["stocks"]], ["005930", "0010S0"])
        stock = context["stocks"][0]
        self.assertEqual(stock["daily_prices"][0]["trade_date"], "2026-11-19")
        self.assertEqual(stock["screen_quote"]["values"]["price"], 1100)
        self.assertEqual(stock["news"][0]["original_url"], "https://example.test/news")
        self.assertNotIn("abc123", json.dumps(context))
        self.assertIn("[숨김]", stock["news"][0]["current_summary"])
        self.assertEqual(len(read_catalog(self.db, watch=True)), 2)
        with self.assertRaises(ValueError):
            build_context("존재하지않는종목", db_path=self.db)
        with self.assertRaises(ValueError):
            build_context(",".join(f"{i:06}" for i in range(20)), db_path=self.db)

    def test_attachment_switch_and_exact_log_filter(self):
        path = self.root / "bot.log"
        path.write_text("\n".join([
            "09:00 trade.audit code=005930 account_no=12345678 api_key=sk-abcdefghijklmnopqrstuvwxyz123456 accept",
            "09:01 trade.audit code=0059300 wrong_stock",
            "09:02 other code=005930 not_audit",
            "09:03 trade.audit code=0010S0 filled",
        ]), encoding="utf-8")
        lines = tail_audit(path, ["005930"])
        self.assertEqual(len(lines), 1)
        self.assertNotIn("12345678", lines[0])
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz", lines[0])
        result = build_context("005930", db_path=self.db, include_data=False,
                               include_logs=True, log_path=path)
        self.assertNotIn("daily_prices", result["stocks"][0])
        self.assertEqual(result["market"], {})
        self.assertEqual(len(result["audit"]), 1)
        prompt = compose_prompt("질문", result, [
            {"role": "assistant", "content": "실패한답변", "status": "error"},
            {"role": "user", "content": "이전질문", "status": "complete"}])
        self.assertNotIn("실패한답변", prompt)
        self.assertIn("이전질문", prompt)

    def test_conversation_storage_restore_and_delete(self):
        path = self.root / "sessions.db"
        store = SessionStore(path)
        session = store.create("codex", "test-model")
        store.add(session, "user", "시험 질문", context={"stocks": []})
        message = store.add(session, "assistant", "받는 중", status="running")
        reopened = SessionStore(path)
        self.assertEqual(reopened.messages(session)[1]["status"], "interrupted")
        reopened.finish(message, "답변", "complete", {"model": "test-model"})
        reopened.configure(session, "codex", "another-model")
        self.assertEqual(reopened.conversations()[0]["model"], "another-model")
        self.assertEqual(reopened.messages(session)[1]["content"], "답변")
        reopened.delete(session)
        self.assertEqual(reopened.messages(session), [])

    def leader_rankings(self, count=3):
        stamp = datetime.now().astimezone().isoformat(timespec="seconds")
        rows = []
        with closing(sqlite3.connect(self.db)) as db, db:
            for i in range(count):
                code, name = f"{600000+i:06}", f"시험대장{i+1}"
                db.execute("INSERT OR IGNORE INTO stocks(stock_code,stock_name,updated_at) VALUES(?,?,?)",
                           (code, name, stamp))
                rows.append({"code": code, "name": name, "rank": i+1, "price": 1000+i,
                             "rate": 29.9-i/10, "volume": 100+i, "trading_value": (i+1)*100000000})
        return {"finished_at": stamp, "change_rate": rows,
                "trading_value": list(reversed(rows)), "warnings": []}

    def test_leaders_use_today_rankings_and_isolate_previous_analysis(self):
        ranks = self.leader_rankings()
        before = self.db.read_bytes()
        context = build_context("005930", db_path=self.db, today_leaders=True, rankings=ranks)
        self.assertEqual(self.db.read_bytes(), before)
        self.assertEqual({s["stock_code"] for s in context["stocks"]}, {"600000", "600001", "600002"})
        self.assertEqual(context["leader_comparison"]["observed_positive_count"], 3)
        prompt = compose_prompt("오늘 대장", context, [
            {"role": "user", "content": "PREVIOUS_SINGLE_STOCK", "status": "complete"}])
        self.assertNotIn("PREVIOUS_SINGLE_STOCK", prompt)
        self.assertNotIn('"stock_code": "005930"', prompt)
        self.assertIn("시험대장1", prompt)

    def test_leaders_keep_locked_low_volume_and_turnover_candidates(self):
        ranks = self.leader_rankings(20)
        quote = {"copied_at": ranks["finished_at"], "display_market": "KRX",
                 "consecutive_days": 5, "values": {"name": "시험점상", "price": 1000,
                 "upper": 1000, "rate": 29.9, "vol": 10, "acc_value": 10000}}
        context = build_context(db_path=self.db, today_leaders=True, rankings=ranks,
                                live_quotes={"600000": quote})
        candidates = context["leader_comparison"]["candidates"]
        codes = [r["stock_code"] for r in candidates]
        self.assertEqual(len(codes), 12)
        self.assertEqual(len(set(codes)), 12)
        self.assertIn("600000", codes)
        self.assertIn("600019", codes)
        self.assertEqual(candidates[0]["consecutive_days"], 5)
        self.assertIn("연상·상한가·상승률 후보", candidates[0]["selection_reason"])

    def test_leaders_reject_stale_data_and_no_old_target_fallback(self):
        ranks = self.leader_rankings()
        ranks["finished_at"] = "2020-01-01T10:00:00+09:00"
        with self.assertRaisesRegex(ValueError, "당일 후보 자료가 부족"):
            build_context("005930,0010S0", db_path=self.db, today_leaders=True, rankings=ranks,
                          live_quotes={"005930": {"copied_at": "2020-01-01", "values": {"price": 100, "rate": 30}}})
        runner = self.runner()
        widget = AiAssistantWidget(db_path=self.db, session_path=self.root / "no_candidates.db", runner=runner)
        self.widgets.append(widget)
        widget.set_targets(["005930"])
        with patch.object(runner, "run", wraps=runner.run) as run:
            widget._preset("compare")
            wait_until(lambda: not widget._busy)
            run.assert_not_called()
        self.assertIn("당일 후보 자료가 부족", widget.status.text())
        self.assertFalse(widget._session_id)

    def test_mixed_date_formats_include_compact_daily_and_exclude_future(self):
        today = datetime.now().date()
        with closing(sqlite3.connect(self.db)) as db, db:
            for date, price in ((today.strftime("%Y%m%d"), 2222),
                                ((today+timedelta(days=10)).strftime("%Y%m%d"), 9999)):
                db.execute("INSERT INTO daily_prices(trade_date,stock_code,close_price,source,collected_at) "
                           "VALUES(?,'005930',?,'KRX',?)", (date, price, date))
        rows = build_context("005930", db_path=self.db)["stocks"][0]["daily_prices"]
        self.assertEqual(rows[-1]["close_price"], 2222)
        self.assertNotIn(9999, [r["close_price"] for r in rows])

    def test_compare_rescans_on_retry_and_restores_visible_scope(self):
        ranks = self.leader_rankings(4)
        calls = []
        async def scan():
            calls.append("scan")
            await asyncio.sleep(0)
            return {**ranks, "change_rate": ranks["change_rate"][:3],
                    "trading_value": ranks["change_rate"][:3]}
        runner = self.runner()
        widget = AiAssistantWidget(db_path=self.db, session_path=self.root / "compare_ui.db",
                                   runner=runner, market_scan=scan)
        self.widgets.append(widget)
        widget.set_targets(["005930"])
        widget.question.setPlainText("PREVIOUS_SINGLE_STOCK")
        widget._prepare()
        wait_until(lambda: not widget._busy)
        widget.resize(1050, 740)
        widget.show()
        async def finished():
            deadline = time.monotonic()+6
            while widget._busy:
                APP.processEvents()
                self.assertLess(time.monotonic(), deadline)
                await asyncio.sleep(.005)
            APP.processEvents()
        async def scenario():
            with patch.object(runner, "run", wraps=runner.run) as run:
                widget.action_buttons[1].click()
                await finished()
                self.assertNotIn("PREVIOUS_SINGLE_STOCK", run.call_args.args[2])
                self.assertNotIn("005930", widget.targets.text())
                self.assertIn("시험대장1", widget.comparison_scope.text())
                self.assertTrue(widget.comparison_scope.isVisible())
                artifacts = os.environ.get("AI_TEST_ARTIFACT_DIR")
                if artifacts:
                    widget.grab().save(str(Path(artifacts)/"ai_compare.png"))
                ranks["change_rate"] = ranks["change_rate"][1:]
                widget.retry_button.click()
                await finished()
                self.assertEqual(len(calls), 2)
                self.assertNotIn("600000", widget.targets.text())
                self.assertIn("600003", widget.targets.text())
                self.assertNotIn("시험대장1", run.call_args.args[2])
            widget._new_chat()
            widget.history.setCurrentRow(0)
            self.assertIn("시험대장4", widget.comparison_scope.text())
            self.assertTrue(widget.comparison_scope.isVisible())
        asyncio.run(scenario())

    def test_stop_during_market_scan_discards_result(self):
        ranks = self.leader_rankings()
        async def scan():
            await asyncio.sleep(60)
            return ranks
        runner = self.runner()
        widget = AiAssistantWidget(db_path=self.db, session_path=self.root / "scan_stop.db",
                                   runner=runner, market_scan=scan)
        self.widgets.append(widget)
        async def scenario():
            widget._preset("compare")
            await asyncio.sleep(.01)
            widget._stop()
            await asyncio.sleep(.01)
            APP.processEvents()
            self.assertFalse(widget._busy)
            self.assertIsNone(widget._pending_request)
            self.assertFalse(widget._session_id)
            self.assertFalse(runner.busy)
        asyncio.run(scenario())

    def test_market_scan_uses_krx_and_reports_partial_error(self):
        class Rest:
            async def change_rate_rank(self, market):
                self.rate_market = market
                raise RuntimeError("api_key=sk-abcdefghijklmnopqrstuvwxyz123456")
            async def trade_value_rank(self, market):
                self.value_market = market
                return [{"code": "005930"}]
        class Host(AiAssistantTabMixin):
            pass
        host = Host()
        host._rest = Rest()
        result = asyncio.run(host._ai_scan_market())
        self.assertEqual(host._rest.rate_market, "1")
        self.assertEqual(host._rest.value_market, "1")
        self.assertEqual(result["change_rate"], [])
        self.assertTrue(result["trading_value"])
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz", json.dumps(result))

    def test_codex_pipe_and_models(self):
        runner = self.runner()
        results, errors, models, texts = [], [], [], []
        runner.completed.connect(lambda text, meta: results.append((text, meta)))
        runner.failed.connect(errors.append)
        runner.models_ready.connect(models.extend)
        runner.text_changed.connect(texts.append)
        runner.probe("codex")
        wait_until(lambda: not runner.busy)
        self.assertFalse(errors, errors)
        self.assertEqual(models[0]["id"], "test-model")
        self.assertNotIn("private@example", json.dumps(results))
        runner.run("codex", "test-model", "질문; $(echo BAD) & test")
        wait_until(lambda: not runner.busy)
        self.assertFalse(errors, errors)
        self.assertEqual(results[-1][0], "연결 성공")
        self.assertEqual(texts[-1], "연결 성공")
        self.assertEqual(results[-1][1]["usage"]["total"]["totalTokens"], 5)

    def test_claude_stream_has_no_duplicate_and_redacts_errors(self):
        runner = self.runner()
        results, errors = [], []
        runner.completed.connect(lambda text, meta: results.append((text, meta)))
        runner.failed.connect(errors.append)
        runner.run("claude", "sonnet", "질문")
        wait_until(lambda: not runner.busy)
        self.assertEqual(results[-1][0], "연결 성공")
        self.assertEqual(results[-1][1]["total_cost_usd"], 0.001)
        runner.run("claude", "sonnet", "FAIL")
        wait_until(lambda: not runner.busy)
        self.assertEqual(len(errors), 1)
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz", errors[0])

    def test_cancel_and_next_request(self):
        runner = self.runner()
        texts, results = [], []
        runner.text_changed.connect(texts.append)
        runner.completed.connect(lambda text, meta: results.append((text, meta)))
        runner.run("codex", "", "SLOW")
        wait_until(lambda: bool(texts))
        runner.stop()
        self.assertFalse(runner.busy)
        self.assertEqual(results[-1][1]["status"], "interrupted")
        runner.run("claude", "", "다음 질문")
        wait_until(lambda: not runner.busy)
        self.assertEqual(results[-1][0], "연결 성공")

    def test_server_retry_and_fatal_notifications(self):
        runner = self.runner()
        statuses, results, errors = [], [], []
        runner.status_changed.connect(statuses.append)
        runner.completed.connect(lambda text, meta: results.append((text, meta)))
        runner.failed.connect(errors.append)
        runner.run("codex", "", "RETRY_NOTICE")
        wait_until(lambda: not runner.busy)
        self.assertFalse(errors)
        self.assertEqual(results[-1][0], "연결 성공")
        self.assertTrue(any("재시도" in status for status in statuses))
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz", json.dumps(results))
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz", "\n".join(statuses))
        runner.run("codex", "", "FATAL_NOTICE")
        wait_until(lambda: not runner.busy)
        self.assertIn("사용량 한도", errors[-1])

    def test_connection_prepare_timeout(self):
        runner = CliRunner(workspace=self.root / "workspace", command_resolver=lambda provider:
                           [sys.executable, str(self.cli), "--stall-initialize"])
        self.runners.append(runner)
        errors = []
        runner.failed.connect(errors.append)
        runner.run("codex", "", "질문")
        runner._ready_deadline.start(150)
        wait_until(lambda: not runner.busy)
        self.assertIn("연결 준비", errors[-1])
        self.assertIn("마지막 단계", errors[-1])

    def test_visible_progress_before_first_answer_and_stopped_history(self):
        runner = self.runner()
        widget = AiAssistantWidget(db_path=self.db,
            session_path=self.root / "progress_sessions.db", runner=runner)
        self.widgets.append(widget)
        widget.resize(1050, 680)
        widget.show()
        widget.question.setPlainText("HOLD")
        widget._prepare()
        wait_until(lambda: "답변 생성 중" in widget._phase)
        now = time.monotonic()
        widget._work_started = now - 35
        widget._last_signal = now - 12
        with patch("ui.ai_assistant_tab.time.monotonic", return_value=now):
            widget._update_progress()
            APP.processEvents()
        self.assertIn("경과 00:35", widget.status.text())
        self.assertIn("마지막 CLI 응답 12초 전", widget.status.text())
        self.assertIn("아직 답변이 도착하지 않았습니다", widget.status.text())
        self.assertIn("test-model", widget.answer.toPlainText())
        self.assertIn("Codex 답변 생성 중", widget.answer.toPlainText())
        self.assertIn("요청 접수", widget.details.toPlainText())
        self.assertTrue(widget.progress.isVisible())
        self.assertFalse(widget.retry_button.isEnabled())
        self.assertNotIn("PRIVATE_NEVER_SHOW", widget.answer.toPlainText())
        artifacts = os.environ.get("AI_TEST_ARTIFACT_DIR")
        if artifacts:
            folder = Path(artifacts)
            folder.mkdir(parents=True, exist_ok=True)
            widget.details_button.setChecked(True)
            APP.processEvents()
            widget.grab().save(str(folder / "ai_progress.png"))
        widget._stop()
        self.assertFalse(widget._busy)
        self.assertFalse(widget._activity_timer.isActive())
        self.assertFalse(widget.progress.isVisible())
        self.assertTrue(widget.retry_button.isEnabled())
        self.assertIn("중지된 요청", widget.answer.toPlainText())
        self.assertNotIn("답변 대기 중", widget.answer.toPlainText())
        self.assertNotIn("답변이 도착하면", widget.answer.toPlainText())
        messages = widget.store.messages(widget._session_id)
        meta = json.loads(messages[-1]["metadata_json"])
        self.assertTrue(meta["progress_log"])
        self.assertNotIn("PRIVATE_NEVER_SHOW", messages[-1]["metadata_json"])
        widget._new_chat()
        widget.history.setCurrentRow(0)
        self.assertIn("중지된 요청", widget.answer.toPlainText())
        self.assertIn("요청 접수", widget.details.toPlainText())

    def test_retry_last_question_with_fresh_context(self):
        runner = self.runner()
        widget = AiAssistantWidget(db_path=self.db,
            session_path=self.root / "retry_sessions.db", runner=runner)
        self.widgets.append(widget)
        widget.set_targets(["005930"])
        widget.question.setPlainText("같은 질문")
        widget._prepare()
        wait_until(lambda: not widget._busy)
        widget.set_targets(["0010S0"])
        widget.retry_button.click()
        wait_until(lambda: not widget._busy)
        messages = widget.store.messages(widget._session_id)
        self.assertEqual(len(messages), 4)
        self.assertEqual(messages[2]["content"], "같은 질문")
        self.assertEqual(json.loads(messages[2]["context_json"])["stocks"][0]["stock_code"], "0010S0")

    def test_ui_send_restore_models_and_render(self):
        runner = self.runner()
        path = self.root / "ui_sessions.db"
        widget = AiAssistantWidget(db_path=self.db, session_path=path, runner=runner)
        self.widgets.append(widget)
        widget.resize(1050, 680)
        widget.show()
        widget.set_targets(["005930"])
        widget.question.setPlainText("종목 설명")
        widget._prepare()
        wait_until(lambda: not widget._busy)
        messages = widget.store.messages(widget._session_id)
        self.assertEqual([m["role"] for m in messages], ["user", "assistant"])
        self.assertEqual(messages[-1]["content"], "연결 성공")
        self.assertTrue(json.loads(messages[0]["context_json"])["stocks"])
        self.assertIn("연결 성공", widget.answer.toPlainText())
        saved_id = widget._session_id
        widget.model.setEditText("custom-model")
        widget.question.setPlainText("이어지는 질문")
        widget._prepare()
        wait_until(lambda: not widget._busy)
        self.assertEqual(widget._session_id, saved_id)
        self.assertEqual(len(widget.store.messages(saved_id)), 4)
        widget._new_chat()
        widget.history.setCurrentRow(0)
        self.assertEqual(widget._session_id, saved_id)
        self.assertEqual(widget._requested_model(), "custom-model")
        self.assertEqual(widget.targets.text(), "005930")
        self.assertIsNone(widget.answer.loadResource(2, None))
        widget._messages[-1]["content"] = (
            "**자료 기준: 2026-11-20 14:30**\n\n"
            "시험전자와 시험신규의 저장된 자료를 비교했습니다.\n\n"
            "| 종목 | 확인된 움직임 | 다음 확인 |\n|---|---|---|\n"
            "| 시험전자 | 거래량 증가 | 당일 고점 유지 여부 |\n"
            "| 시험신규 | 일봉 자료 부족 | 자료 수집 후 비교 |\n\n"
            "새로운 계약 체결은 확인되지 않았습니다. "
            "[첨부 기사](https://example.test/news)를 확인해 주세요.")
        widget._paint()
        for dark in (False, True):
            palette = QPalette()
            if dark:
                palette.setColor(QPalette.ColorRole.Window, "#202124")
                palette.setColor(QPalette.ColorRole.Base, "#17181a")
                palette.setColor(QPalette.ColorRole.Text, "#eeeeee")
                palette.setColor(QPalette.ColorRole.WindowText, "#eeeeee")
                palette.setColor(QPalette.ColorRole.ButtonText, "#eeeeee")
                palette.setColor(QPalette.ColorRole.Button, "#303134")
                palette.setColor(QPalette.ColorRole.PlaceholderText, "#a0a0a0")
            widget.setPalette(palette)
            widget._paint()
            APP.processEvents()
            image = widget.grab()
            self.assertFalse(image.isNull())
            self.assertIn("<table", widget.answer.toHtml())
            image.save(str(self.root / ("dark.png" if dark else "light.png")))
            artifacts = os.environ.get("AI_TEST_ARTIFACT_DIR")
            if artifacts:
                folder = Path(artifacts)
                folder.mkdir(parents=True, exist_ok=True)
                image.save(str(folder / ("ai_dark.png" if dark else "ai_light.png")))

    def test_stop_during_context_does_not_send_stale_selection(self):
        runner = self.runner()
        widget = AiAssistantWidget(db_path=self.db,
            session_path=self.root / "cancel_sessions.db", runner=runner)
        self.widgets.append(widget)
        entered = threading.Event()
        original = build_context
        def delayed(**kwargs):
            if kwargs["targets"] == "005930":
                entered.set()
                time.sleep(0.15)
            return original(**kwargs)
        with patch("ui.ai_assistant_tab.build_context", delayed):
            widget.set_targets(["005930"])
            widget.question.setPlainText("취소할 질문")
            widget._prepare()
            wait_until(entered.is_set)
            widget._stop()
            widget.set_targets(["0010S0"])
            widget.question.setPlainText("새 종목 질문")
            widget._prepare()
            wait_until(lambda: not widget._busy and not widget._jobs)
        messages = widget.store.messages(widget._session_id)
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0]["content"], "새 종목 질문")
        self.assertEqual(json.loads(messages[0]["context_json"])["stocks"][0]["stock_code"], "0010S0")

    def test_selection_import_preserves_alphanumeric_codes(self):
        class Harness(AiAssistantTabMixin, QMainWindow):
            pass
        host = Harness()
        host._tabs = QTabWidget()
        host._tabs.addTab(QTableWidget(), "종목뉴스·종토방")
        host._tabs.addTab(QTableWidget(), "AI 분석")
        host._news_watch_table = QTableWidget(2, 2)
        for row, code in enumerate(["005930", "0010S0"]):
            item = QTableWidgetItem(code)
            item.setData(Qt.ItemDataRole.UserRole + 2, code)
            host._news_watch_table.setItem(row, 0, item)
        host._news_watch_table.selectAll()
        self.assertEqual(host._ai_selected_codes(), ["005930", "0010S0"])
        host._tabs.setCurrentIndex(1)
        host._ai_source_tab = "종목뉴스·종토방"
        self.assertEqual(host._ai_selected_codes(), ["005930", "0010S0"])
        host.close()


def live(probe):
    with tempfile.TemporaryDirectory(prefix="trading_ai_live_") as directory:
        for provider in ("codex", "claude"):
            runner = CliRunner(workspace=Path(directory) / provider)
            result, error = [], []
            runner.completed.connect(lambda text, meta: result.append((text, meta)))
            runner.failed.connect(error.append)
            if probe:
                runner.probe(provider)
            else:
                runner.run(provider, "", "도구를 사용하지 말고 '연결 성공' 네 글자만 답해 주세요.")
            try:
                wait_until(lambda: not runner.busy, timeout=55)
                if error:
                    print(provider, "FAILED", error[0])
                elif result:
                    print(provider, "OK", result[0][0] if not probe else result[0][1].get("status"))
                else:
                    print(provider, "NO RESULT")
            finally:
                runner.shutdown()
                APP.processEvents()


if __name__ == "__main__":
    if "--live-probe" in sys.argv or "--live-smoke" in sys.argv:
        live("--live-probe" in sys.argv)
    else:
        unittest.main()
