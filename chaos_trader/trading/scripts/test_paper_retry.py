"""Offline retry/restart and overlapping-process regressions on real SQLite."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import chaos_paper_autopilot as a
from test_chaos_paper_autopilot import (
    MINT, config_for, seed_wallet_event_db, source_gap_payload_fixture,
    sweep_payload_fixture,
)


class RetryTests(unittest.TestCase):
    def runner(self, root):
        cfg = config_for(root)
        smart = root / 'smart.sqlite'
        seed_wallet_event_db(smart, run_id='elite-proof', observed_at=datetime.now(timezone.utc) - timedelta(minutes=5), wallets=2)
        cfg.raw['paths']['smart_wallet_sqlite'] = str(smart)
        return a.PaperAutopilotRunner(cfg)

    def tick(self, runner, *, payload=None, price=0.001, analyze_top=1):
        if payload is None:
            payload = source_gap_payload_fixture()
        with patch.object(a, 'sweep_payload', return_value=sweep_payload_fixture()), patch.object(a.x_provider, 'provider_name', return_value='xai'), patch.object(runner, 'run_analyze', return_value=payload) as analyze, patch.object(runner, 'fetch_market', return_value={'price_usd': price, 'liquidity_usd': 60000}):
            out = runner.run_once(analyze_top=analyze_top, with_x=True)
        return out, analyze.call_count

    def counts(self, runner):
        with runner.connect() as con:
            return tuple(con.execute('SELECT COUNT(*) FROM ' + t).fetchone()[0] for t in ('paper_positions', 'paper_fills', 'paper_fees'))

    def test_failed_entry_consumes_durable_hourly_and_daily_reservations(self):
        for cap in ('max_per_hour', 'max_per_day'):
            with self.subTest(cap=cap), tempfile.TemporaryDirectory() as td:
                runner = self.runner(Path(td))
                runner.config.raw['deep_analyze'][cap] = 1
                with runner.connect() as con:
                    con.execute("CREATE TRIGGER fail_fee BEFORE INSERT ON paper_fees BEGIN SELECT RAISE(ABORT, 'retry fee fault'); END")
                payload = source_gap_payload_fixture()
                payload['x_request_made'] = True
                out, calls = self.tick(runner, payload=payload)
                self.assertFalse(out['ok'])
                self.assertEqual(calls, 1)
                self.assertEqual(self.counts(runner), (0, 0, 0))
                restarted = a.PaperAutopilotRunner(runner.config)
                with restarted.connect() as con:
                    self.assertEqual(restarted.get_counter(con, 'deep_analyze'), 1)
                    self.assertEqual(restarted.get_counter(con, 'x_search'), 1)
                    for name in ('deep_analyze', 'x_search'):
                        self.assertEqual(con.execute('SELECT COUNT(*) FROM events WHERE event_type=?', (name,)).fetchone()[0], 1)
                    con.execute('DROP TRIGGER fail_fee')
                out, calls = self.tick(restarted, payload=payload)
                self.assertEqual(calls, 0)
                self.assertEqual(out['decisions'][0]['decision'], 'budget_exhausted')
                self.assertEqual(self.counts(restarted), (0, 0, 0))

    def test_analyzer_exception_keeps_reservation_before_call(self):
        with tempfile.TemporaryDirectory() as td:
            runner = self.runner(Path(td))
            def fail(mint, **kwargs):
                # Independent connection proves durability before crossing the boundary.
                with runner.connect() as observer:
                    self.assertEqual(runner.get_counter(observer, 'deep_analyze'), 1)
                    self.assertEqual(runner.get_counter(observer, 'x_search'), 1)
                raise RuntimeError('no analyzer receipt')
            with patch.object(a, 'sweep_payload', return_value=sweep_payload_fixture()), patch.object(a.x_provider, 'provider_name', return_value='xai'), patch.object(runner, 'run_analyze', side_effect=fail):
                out = runner.run_once(analyze_top=1, with_x=True)
            self.assertFalse(out['ok'])
            with runner.connect() as con:
                self.assertEqual(runner.get_counter(con, 'deep_analyze'), 1)
                self.assertEqual(runner.get_counter(con, 'x_search'), 1)

    def test_x_caps_survive_failed_accounting_and_successful_retry(self):
        for cap in ('max_per_hour', 'max_per_day'):
            with self.subTest(cap=cap), tempfile.TemporaryDirectory() as td:
                runner = self.runner(Path(td))
                runner.config.raw['x_research'][cap] = 1
                with runner.connect() as con:
                    con.execute("CREATE TRIGGER fail_fee BEFORE INSERT ON paper_fees BEGIN SELECT RAISE(ABORT, 'retry fee fault'); END")
                enabled = []
                def analyze(mint, *, x_enabled, **kwargs):
                    enabled.append(x_enabled)
                    payload = source_gap_payload_fixture()
                    payload['x_request_made'] = x_enabled
                    return payload
                with patch.object(a, 'sweep_payload', return_value=sweep_payload_fixture()), patch.object(a.x_provider, 'provider_name', return_value='xai'):
                    for attempt in range(2):
                        runner = a.PaperAutopilotRunner(runner.config)
                        with patch.object(runner, 'run_analyze', side_effect=analyze):
                            self.assertFalse(runner.run_once(analyze_top=1, with_x=True)['ok'])
                    with runner.connect() as con:
                        self.assertEqual(runner.get_counter(con, 'deep_analyze'), 2)
                        self.assertEqual(runner.get_counter(con, 'x_search'), 1)
                        con.execute('DROP TRIGGER fail_fee')
                    with patch.object(runner, 'run_analyze', side_effect=analyze):
                        self.assertTrue(runner.run_once(analyze_top=1, with_x=True)['ok'])
                self.assertEqual(enabled, [True, False, False])
                self.assertEqual(self.counts(runner), (1, 1, 1))
                for repeat in range(2):
                    out, calls = self.tick(a.PaperAutopilotRunner(runner.config))
                    self.assertTrue(out['ok'])
                    self.assertEqual(calls, 0)
                    self.assertEqual(self.counts(runner), (1, 1, 1))

    def test_no_request_receipt_releases_only_x_reservation(self):
        for receipt in (False, None, 'false'):
            with self.subTest(receipt=receipt), tempfile.TemporaryDirectory() as td:
                runner = self.runner(Path(td))
                payload = source_gap_payload_fixture()
                payload['x_request_made'] = receipt
                self.tick(runner, payload=payload)
                with runner.connect() as con:
                    self.assertEqual(runner.get_counter(con, 'deep_analyze'), 1)
                    self.assertEqual(runner.get_counter(con, 'x_search'), 0 if receipt is False else 1)
                    self.assertEqual(con.execute("SELECT COUNT(*) FROM events WHERE event_type='x_search'").fetchone()[0], 0 if receipt is False else 1)

    def test_restart_lifecycle_and_identical_replay(self):
        with tempfile.TemporaryDirectory() as td:
            runner = self.runner(Path(td))
            out, calls = self.tick(runner)
            self.assertTrue(out['ok'], out)
            self.assertEqual(self.counts(runner), (1, 1, 1))
            for price, state, fills in ((0.001, 'PAPER_OPEN', 1), (0.002, 'PAPER_TRIMMED', 2), (0.0007, 'PAPER_CLOSED', 3)):
                runner = a.PaperAutopilotRunner(runner.config)
                for replay in range(2):
                    out, calls = self.tick(runner, price=price)
                    self.assertTrue(out['ok'], out)
                    self.assertEqual(self.counts(runner), (1, fills, fills))
                    with runner.connect() as con:
                        row = con.execute('SELECT * FROM paper_positions').fetchone()
                        self.assertEqual(row['state'], state)
                        self.assertEqual(runner.get_counter(con, 'deep_analyze'), 1)
                        self.assertEqual(con.execute('PRAGMA foreign_key_check').fetchall(), [])
                        self.assertEqual(con.execute('SELECT COUNT(DISTINCT fill_id) FROM paper_fills').fetchone()[0], fills)
                        if state == 'PAPER_CLOSED':
                            self.assertEqual(row['remaining_pct'], 0)

    def test_overlapping_processes_do_not_exceed_analysis_or_entry_caps(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            runner = self.runner(root)
            runner.config.raw['deep_analyze']['max_per_day'] = 1
            runner.config.raw['deep_analyze']['max_per_hour'] = 1
            with runner.connect():
                pass
            self.overlap(root, runner)
            self.assertEqual(len((root/'requests').read_text().splitlines()), 1)
            self.assertEqual(self.counts(runner), (1, 1, 1))
            with runner.connect() as con:
                self.assertEqual(runner.get_counter(con, 'deep_analyze'), 1)

    def overlap(self, root, runner, price=.001, analyze_top=1, sync_analysis=False):
            cfg = root / 'config.json'
            cfg.write_text(json.dumps(runner.config.raw))
            # Both real runners reach discovery together; only external boundaries are fixtures.
            code = '''
import json, sys, time
from pathlib import Path
from unittest.mock import patch
import chaos_paper_autopilot as a
from test_chaos_paper_autopilot import source_gap_payload_fixture, sweep_payload_fixture
root=Path(sys.argv[1]); index=sys.argv[2]
cfg=json.loads((root/'config.json').read_text())
r=a.PaperAutopilotRunner(a.RunnerConfig(cfg, Path(cfg['paths']['sqlite'])))
price=float(sys.argv[3]); analyze_top=int(sys.argv[4]); sync_analysis=sys.argv[5]=="True"
with r.connect(): pass
(root/('ready'+index)).touch()
deadline=time.monotonic()+10
while not all((root/('ready'+i)).exists() for i in ('0','1')):
    if time.monotonic()>deadline: raise RuntimeError('barrier timeout')
    time.sleep(.01)
def sweep(**kwargs):
    return sweep_payload_fixture()
def analyze(*args, **kwargs):
    with (root/'requests').open('a') as f: f.write(index+'\\n')
    if sync_analysis:
        deadline=time.monotonic()+10
        while len((root/'requests').read_text().splitlines()) < 2:
            if time.monotonic()>deadline: raise RuntimeError('analysis barrier timeout')
            time.sleep(.01)
    payload=source_gap_payload_fixture()
    payload['x_request_made']=kwargs['x_enabled']
    return payload
with patch.object(a.x_provider,'provider_name',return_value='xai'), patch.object(a,'sweep_payload',side_effect=sweep), patch.object(r,'run_analyze',side_effect=analyze), patch.object(r,'fetch_market',return_value={'price_usd':price,'liquidity_usd':60000}):
    print(json.dumps(r.run_once(analyze_top=analyze_top, with_x=True)))
'''
            procs = [subprocess.Popen([sys.executable, '-W', 'error::ResourceWarning', '-c', code, str(root), str(i), str(price), str(analyze_top), str(sync_analysis)], cwd=Path(__file__).parent, env={**os.environ, 'CHAOS_HOME': str(root), 'PROFILE': str(root)}, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for i in range(2)]
            try:
                results = [proc.communicate(timeout=40) for proc in procs]
                self.assertEqual([proc.returncode for proc in procs], [0, 0], repr(results))
                for stdout, stderr in results:
                    self.assertTrue(json.loads(stdout)['ok'], stdout)
            finally:
                for proc in procs:
                    if proc.poll() is None:
                        proc.kill()
                    proc.communicate()
            for i in range(2):
                (root / ('ready' + str(i))).unlink()

    def test_overlapping_processes_share_x_caps(self):
        for cap in ('max_per_hour', 'max_per_day'):
            with self.subTest(cap=cap), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                runner = self.runner(root)
                runner.config.raw['x_research'][cap] = 1
                with runner.connect():
                    pass
                self.overlap(root, runner, sync_analysis=True)
                self.assertEqual(self.counts(runner), (1, 1, 1))
                with runner.connect() as con:
                    self.assertEqual(runner.get_counter(con, 'deep_analyze'), 2)
                    self.assertEqual(runner.get_counter(con, 'x_search'), 1)
                    self.assertEqual(con.execute("SELECT COUNT(*) FROM events WHERE event_type='x_search'").fetchone()[0], 1)

    def test_overlapping_process_lifecycle_replay(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            runner = self.runner(root)
            with runner.connect():
                pass
            self.overlap(root, runner, sync_analysis=True)
            self.assertEqual(self.counts(runner), (1, 1, 1))
            for price, count in ((.001, 1), (.002, 2), (.0007, 3)):
                for repeat in range(2):
                    self.overlap(root, runner, price=price, analyze_top=0)
                    self.assertEqual(self.counts(runner), (1, count, count))
            with runner.connect() as con:
                self.assertEqual(con.execute('SELECT remaining_pct FROM paper_positions').fetchone()[0], 0)
                self.assertEqual(con.execute('PRAGMA foreign_key_check').fetchall(), [])
