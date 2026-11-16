"""Real SQLite fault injection at accounting boundaries; no provider calls."""
import test_chaos_paper_autopilot as fixtures
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import chaos_paper_autopilot as autopilot
from paper_autopilot_config_check import load_config, validate
from test_chaos_paper_autopilot import (
    MINT, config_for, seed_wallet_event_db,
    source_gap_payload_fixture, sweep_payload_fixture,
)


class ReliabilityTests(unittest.TestCase):
    def snapshot(self, con):
        return {table: [tuple(r) for r in con.execute(f'SELECT * FROM {table}')]
                for table in ('paper_positions', 'paper_fills', 'paper_fees', 'budget_counters')}

    def test_monitor_failed_close_and_trim_roll_back_and_retry(self):
        for price in (0.0007, 0.0032):
            for fault in ('fee', 'event'):
                with self.subTest(price=price, fault=fault), tempfile.TemporaryDirectory() as td:
                    runner = autopilot.PaperAutopilotRunner(config_for(Path(td)))
                    fixtures.PaperAutopilotRunnerTests()._open_position(runner)
                    with runner.connect() as con:
                        before = self.snapshot(con)
                        if fault == 'fee':
                            con.execute("CREATE TRIGGER fail_transition BEFORE INSERT ON paper_fees WHEN NEW.fee_type='exit' BEGIN SELECT RAISE(ABORT, 'injected fee failure'); END")
                        else:
                            con.execute("CREATE TRIGGER fail_transition BEFORE INSERT ON events WHEN NEW.event_type IN ('paper_exit','paper_trim') BEGIN SELECT RAISE(ABORT, 'injected event failure'); END")
                        con.commit()
                        market = {'price_usd': price, 'market_cap': 180000, 'liquidity_usd': 60000}
                        with patch.object(runner, 'fetch_market', return_value=market):
                            self.assertEqual(len(runner.monitor_positions_once(con)), 1)
                        self.assertEqual(self.snapshot(con), before)
                        self.assertEqual(con.execute("SELECT COUNT(*) FROM events WHERE event_type='position_monitor_error'").fetchone()[0], 1)
                        con.execute('DROP TRIGGER fail_transition')
                        con.commit()
                        with patch.object(runner, 'fetch_market', return_value=market):
                            runner.monitor_positions_once(con)
                        self.assertNotEqual(self.snapshot(con), before)
                        self.assertEqual(con.execute('SELECT COUNT(*) FROM paper_fills').fetchone()[0], con.execute('SELECT COUNT(*) FROM paper_fees').fetchone()[0])

    def test_candidate_entry_failure_rolls_back_all_accounting(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = config_for(root)
            smart = root / 'smart.sqlite'
            seed_wallet_event_db(smart, run_id='elite-proof', observed_at=datetime.now(timezone.utc) - timedelta(minutes=5), wallets=2)
            cfg.raw['paths']['smart_wallet_sqlite'] = str(smart)
            runner = autopilot.PaperAutopilotRunner(cfg)
            with runner.connect() as con:
                con.execute("CREATE TRIGGER fail_entry BEFORE INSERT ON paper_fees BEGIN SELECT RAISE(ABORT, 'injected entry fee failure'); END")
                con.commit()
                before = self.snapshot(con)
            with patch.object(autopilot, 'sweep_payload', return_value=sweep_payload_fixture()), patch.object(runner, 'run_analyze', return_value=source_gap_payload_fixture()):
                out = runner.run_once(limit=1, analyze_top=1)
            with runner.connect() as con:
                after = self.snapshot(con)
                self.assertEqual(after.pop('budget_counters'), [(autopilot.today_utc(), 'deep_analyze', 1)])
                before.pop('budget_counters')
                self.assertEqual(after, before)
                self.assertEqual(con.execute("SELECT COUNT(*) FROM events WHERE event_type='deep_analyze_error'").fetchone()[0], 1)
                self.assertNotEqual(con.execute('SELECT state FROM candidates WHERE mint=?', (MINT,)).fetchone()[0], 'PAPER_OPEN')
            self.assertFalse(out['ok'])
            self.assertEqual(out['stages']['candidates']['failed'], 1)

    def test_missing_mark_is_not_counted_as_checked(self):
        with tempfile.TemporaryDirectory() as td:
            runner = autopilot.PaperAutopilotRunner(config_for(Path(td)))
            fixtures.PaperAutopilotRunnerTests()._open_position(runner)
            with patch.object(autopilot, 'sweep_payload', return_value={'ok': True, 'candidates': []}), patch.object(runner, 'fetch_market', return_value={}):
                result = runner.run_once()
            self.assertFalse(result['ok'])
            self.assertEqual(result['open_positions_checked'], 0)
            self.assertEqual(result['open_positions_attempted'], 1)
            with runner.connect() as con:
                self.assertEqual(con.execute('SELECT state FROM paper_positions').fetchone()[0], 'PAPER_OPEN')

    def test_expired_budget_reports_deferred_not_failed(self):
        with tempfile.TemporaryDirectory() as td:
            runner = autopilot.PaperAutopilotRunner(config_for(Path(td)))
            with patch.object(autopilot, 'sweep_payload', return_value=sweep_payload_fixture()):
                result = runner.run_once(analyze_top=1, budget_seconds=-1)
            self.assertTrue(result['ok'])
            self.assertEqual(result['health'], 'deferred')
            self.assertEqual(result['stages']['candidates']['attempted'], 0)
            self.assertEqual(result['stages']['candidates']['deferred'], 1)

    def test_monitor_failure_preserves_prior_completed_close(self):
        with tempfile.TemporaryDirectory() as td:
            runner = autopilot.PaperAutopilotRunner(config_for(Path(td)))
            fixtures.PaperAutopilotRunnerTests()._open_position(runner)
            with runner.connect() as con:
                con.execute("UPDATE paper_positions SET mint='prior-mint', opened_at_utc='2026-01-01T00:00:00+00:00'")
                con.execute("UPDATE candidates SET mint='prior-mint'")
                con.commit()
            fixtures.PaperAutopilotRunnerTests()._open_position(runner)
            with runner.connect() as con:
                target = con.execute('SELECT id FROM paper_positions WHERE mint=?', (MINT,)).fetchone()[0]
                # The first position closes successfully; only the later one fails.
                con.execute("CREATE TRIGGER fail_later BEFORE INSERT ON paper_fees WHEN NEW.fee_type='exit' AND NEW.position_id='" + target + "' BEGIN SELECT RAISE(ABORT, 'later failure'); END")
                con.commit()
                health = {}
                with patch.object(runner, 'fetch_market', return_value={'price_usd': 0.0007}):
                    runner.monitor_positions_once(con, health=health)
                states = {r['mint']: r['state'] for r in con.execute('SELECT mint,state FROM paper_positions')}
                self.assertEqual(states, {'prior-mint': 'PAPER_CLOSED', MINT: 'PAPER_OPEN'})
                self.assertEqual(health, {'attempted': 2, 'succeeded': 1, 'failed': 1, 'deferred': 0})
                self.assertEqual(con.execute("SELECT COUNT(*) FROM paper_fills WHERE side='exit'").fetchone()[0], 1)
                self.assertEqual(con.execute("SELECT COUNT(*) FROM paper_fees WHERE fee_type='exit'").fetchone()[0], 1)

    def test_discovery_failure_and_partial_payload_are_unhealthy(self):
        for payload in ({'ok': False, 'candidates': []}, {'ok': True, 'candidates': [], 'errors': ['provider failed']}):
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as td:
                runner = autopilot.PaperAutopilotRunner(config_for(Path(td)))
                with patch.object(autopilot, 'sweep_payload', return_value=payload):
                    result = runner.run_once()
                self.assertFalse(result['ok'])
                self.assertEqual(result['stages']['discovery']['failed'], 1)
        with tempfile.TemporaryDirectory() as td:
            runner = autopilot.PaperAutopilotRunner(config_for(Path(td)))
            with patch.object(autopilot, 'sweep_payload', side_effect=RuntimeError('discovery unavailable')):
                result = runner.run_once()
            self.assertFalse(result['ok'])
            self.assertEqual(result['error_count'], 1)
            with runner.connect() as con:
                self.assertEqual(con.execute("SELECT COUNT(*) FROM events WHERE event_type='discovery_error'").fetchone()[0], 1)

    def test_loop_keeps_prior_failed_cycle_in_aggregate_health(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = config_for(Path(td))
            cfg.raw['loop']['discovery_interval_sec'] = 0
            runner = autopilot.PaperAutopilotRunner(cfg)
            fixtures.PaperAutopilotRunnerTests()._open_position(runner)
            with patch.object(autopilot, 'sweep_payload', return_value={'ok': True, 'candidates': []}), patch.object(runner, 'fetch_market', side_effect=[RuntimeError('first cycle'), {'price_usd': 0.001, 'liquidity_usd': 60000}]), redirect_stdout(io.StringIO()):
                result = runner.run_loop(max_cycles=2)
            self.assertFalse(result['ok'])
            self.assertEqual(result['failed_cycles'], 1)
            self.assertTrue(result['last']['ok'])

    def test_cli_subprocess_returns_failure_and_json_health(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = config_for(root)
            cfg.raw['boundary'].update(no_swap_links=True, no_live_alerts=True)
            runner = autopilot.PaperAutopilotRunner(cfg)
            fixtures.PaperAutopilotRunnerTests()._open_position(runner)
            path = root / 'config.json'
            path.write_text(json.dumps(cfg.raw))
            # Only external observations are replaced; actual main, SQLite and accounting run.
            code = "from unittest.mock import patch; import chaos_paper_autopilot as a; " \
                   "p=patch.object(a,'sweep_payload',return_value={'ok':True,'candidates':[]}); p.start(); " \
                   "q=patch.object(a.PaperAutopilotRunner,'fetch_market',side_effect=RuntimeError('offline mark')); q.start(); a.main()"
            for raw in (True, False):
                result = subprocess.run([sys.executable, '-W', 'error::ResourceWarning', '-c', code, '--config', str(path), '--once'] + (['--raw'] if raw else []), cwd=Path(__file__).parent, env={**os.environ, 'CHAOS_HOME': str(root)}, capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertNotIn('Traceback', result.stderr)
                if raw:
                    payload = json.loads(result.stdout)
                    self.assertFalse(payload['ok'])
                    self.assertEqual(payload['stages']['monitor']['failed'], 1)
                else:
                    self.assertIn('Errors: 1', result.stdout)

    def test_monitor_failure_is_unhealthy_in_cli_and_raw(self):
        for raw in (False, True):
            with self.subTest(raw=raw), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                cfg = config_for(root)
                cfg.raw['boundary'].update(no_swap_links=True, no_live_alerts=True)
                runner = autopilot.PaperAutopilotRunner(cfg)
                fixtures.PaperAutopilotRunnerTests()._open_position(runner)
                path = root / 'config.json'
                path.write_text(json.dumps(cfg.raw))
                output = io.StringIO()
                argv = ['autopilot', '--config', str(path), '--once'] + (['--raw'] if raw else [])
                with patch('sys.argv', argv), patch.object(autopilot, 'sweep_payload', return_value={'ok': True, 'candidates': []}), patch.object(autopilot.PaperAutopilotRunner, 'fetch_market', side_effect=RuntimeError('offline monitor failure')), redirect_stdout(output), self.assertRaises(SystemExit) as raised:
                    autopilot.main()
                self.assertEqual(raised.exception.code, 1)
                if raw:
                    result = json.loads(output.getvalue())
                    self.assertFalse(result['ok'])
                    self.assertEqual(result['stages']['monitor']['failed'], 1)
                    self.assertEqual(result['open_positions_checked'], 0)
                else:
                    self.assertIn('Errors: 1', output.getvalue())


class ConfigReliabilityTests(unittest.TestCase):
    def config(self):
        return load_config(Path(__file__).resolve().parents[1] / 'config' / 'paper_autopilot.yaml')

    def test_invalid_values_are_rejected_without_exception(self):
        for section, key, value in [('budgets', 'max_open_positions', -1), ('budgets', 'max_open_positions', True), ('budgets', 'max_open_positions', 1.5), ('exit', 'stop_pct_conviction', 25), ('exit', 'stop_pct_low_cap', float('nan')), ('deep_analyze', 'max_per_day', 'broken')]:
            with self.subTest(section=section, key=key, value=value):
                config = self.config()
                config[section][key] = value
                self.assertFalse(validate(config)['ok'])

    def test_malformed_sections_and_root_are_reported(self):
        for section in ('loop', 'boundary', 'paths', 'budgets', 'exit', 'llm', 'x_research', 'deep_analyze'):
            for value in ('invalid', [], None):
                with self.subTest(section=section, value=value):
                    config = self.config()
                    config[section] = value
                    self.assertFalse(validate(config)['ok'])
        for config in ([], 'invalid', None):
            self.assertFalse(validate(config)['ok'])

    def test_unsupported_controls_are_rejected_even_when_disabled(self):
        for section, key, value in [('loop', 'position_refresh_sec', 5), ('exit', 'exit_on_gate_flip', []), ('exit', 'exit_on_flow_conversion', [])]:
            with self.subTest(key=key):
                config = self.config()
                config[section][key] = value
                result = validate(config)
                self.assertFalse(result['ok'])
                self.assertTrue(any(section + '.' + key in error for error in result['errors']))

    def test_shipped_config_is_valid(self):
        self.assertTrue(validate(self.config())['ok'])
