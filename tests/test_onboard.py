import os
import tempfile
import unittest
from pathlib import Path

from chaos_trader.onboard import onboard


class OnboardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"

    def tearDown(self):
        self.tmp.cleanup()

    def test_fresh_home_gets_state_env_and_seed_but_no_code(self):
        onboard(self.home, rpc_url="https://api.mainnet-beta.solana.com", helius_key=None)
        self.assertFalse((self.home / "trading" / "scripts").exists())
        for f in ("trading/config/roster.json", "trading/config/paper_autopilot.yaml",
                  "trading/config/paper_autopilot.defaults.yaml", "trading/db/smart_wallets_schema.sql"):
            self.assertTrue((self.home / f).is_file(), f)
        for d in ("trading/db", "trading/reports", "trading/alpha", "trading/alpha/secondary", "trading/alpha/mint_scores", "trading/state"):
            self.assertTrue((self.home / d).is_dir(), d)
        env = (self.home / ".env").read_text(encoding="utf-8")
        self.assertIn("SOLANA_RPC_URL=https://api.mainnet-beta.solana.com", env)
        self.assertNotIn("HELIUS_API_KEY=", env)

    def test_fresh_home_holds_state_only(self):
        onboard(self.home, rpc_url="https://api.mainnet-beta.solana.com", helius_key=None)
        found = sorted(p.relative_to(self.home).as_posix() for p in self.home.rglob("*"))
        self.assertEqual(found, sorted([
            ".env", "trading", "trading/config", "trading/config/roster.json", "trading/config/paper_autopilot.yaml",
            "trading/config/paper_autopilot.defaults.yaml", "trading/db", "trading/db/smart_wallets_schema.sql",
            "trading/reports", "trading/alpha", "trading/alpha/secondary", "trading/alpha/mint_scores", "trading/state",
        ]))

    def test_onboard_on_a_set_up_home_refuses_and_names_update(self):
        onboard(self.home, rpc_url="https://api.mainnet-beta.solana.com", helius_key=None)
        with self.assertRaises(SystemExit) as ctx:
            onboard(self.home, rpc_url="https://api.mainnet-beta.solana.com", helius_key=None)
        self.assertEqual(str(ctx.exception), f"{self.home.resolve()} is already set up. Run `chaos update` to refresh the defaults, or pick another CHAOS_HOME.")

    def test_update_on_a_home_that_is_not_set_up_refuses_and_names_onboard(self):
        with self.assertRaises(SystemExit) as ctx:
            onboard(self.home, rpc_url=None, helius_key=None, update=True)
        self.assertEqual(str(ctx.exception), f"{self.home.resolve()} is not set up. Run `chaos onboard` first.")
        self.assertFalse(self.home.exists())

    def test_onboard_into_a_directory_with_other_files_leaves_them_alone(self):
        self.home.mkdir(parents=True)
        soul = self.home / "SOUL.md"
        soul.write_text("# who I am\n", encoding="utf-8")
        onboard(self.home, rpc_url="https://api.mainnet-beta.solana.com", helius_key=None)
        self.assertEqual(soul.read_text(encoding="utf-8"), "# who I am\n")
        self.assertTrue((self.home / "trading" / "config" / "roster.json").is_file())

    def test_update_restores_a_missing_schema_and_leaves_a_missing_paper_config_missing(self):
        onboard(self.home, rpc_url="https://api.mainnet-beta.solana.com", helius_key=None)
        schema = self.home / "trading" / "db" / "smart_wallets_schema.sql"
        cfg = self.home / "trading" / "config" / "paper_autopilot.yaml"
        schema.unlink()
        cfg.unlink()
        onboard(self.home, rpc_url=None, helius_key=None, update=True)
        self.assertTrue(schema.is_file())
        self.assertFalse(cfg.exists())

    def test_helius_key_written_when_given(self):
        onboard(self.home, rpc_url=None, helius_key="k123")
        env = (self.home / ".env").read_text(encoding="utf-8")
        self.assertIn("HELIUS_API_KEY=k123", env)

    def test_update_preserves_runtime_and_env(self):
        onboard(self.home, rpc_url="https://api.mainnet-beta.solana.com", helius_key=None)
        db = self.home / "trading" / "db" / "paper_autopilot.sqlite"
        db.write_bytes(b"not-really-sqlite-but-must-survive")
        roster = self.home / "trading" / "config" / "roster.json"
        roster.write_text('{"wallets": []}', encoding="utf-8")
        env_before = (self.home / ".env").read_text(encoding="utf-8")
        onboard(self.home, rpc_url=None, helius_key=None, update=True)
        self.assertEqual(db.read_bytes(), b"not-really-sqlite-but-must-survive")
        self.assertEqual(roster.read_text(encoding="utf-8"), '{"wallets": []}')
        self.assertEqual((self.home / ".env").read_text(encoding="utf-8"), env_before)

    def test_update_keeps_edited_paper_config_and_refreshes_defaults(self):
        onboard(self.home, rpc_url="https://api.mainnet-beta.solana.com", helius_key=None)
        config_dir = self.home / "trading" / "config"
        cfg = config_dir / "paper_autopilot.yaml"
        defaults = config_dir / "paper_autopilot.defaults.yaml"
        self.assertEqual(defaults.read_bytes(), cfg.read_bytes())
        edited = cfg.read_text(encoding="utf-8") + "\n# my edit\n"
        cfg.write_text(edited, encoding="utf-8")
        defaults.write_text("stale", encoding="utf-8")
        onboard(self.home, rpc_url=None, helius_key=None, update=True)
        self.assertEqual(cfg.read_text(encoding="utf-8"), edited)
        self.assertIn("wallet_signal_lane_enabled", defaults.read_text(encoding="utf-8"))
        self.assertNotIn("# my edit", defaults.read_text(encoding="utf-8"))

    def test_refuses_a_home_inside_the_package(self):
        import chaos_trader
        pkg = Path(chaos_trader.__file__).resolve().parent
        with self.assertRaises(SystemExit):
            onboard(pkg / "trading", rpc_url="https://api.mainnet-beta.solana.com", helius_key=None)

    def test_existing_env_is_merged_in_order_and_url_wins_over_key(self):
        self.home.mkdir(parents=True)
        (self.home / ".env").write_text("OTHER=1\nHELIUS_API_KEY=k\n", encoding="utf-8")
        report: list[str] = []
        onboard(self.home, rpc_url="https://rpc.example/x", helius_key="newkey", env_report=report)
        self.assertEqual((self.home / ".env").read_text(encoding="utf-8").splitlines(),
                         ["OTHER=1", "HELIUS_API_KEY=k", "SOLANA_RPC_URL=https://rpc.example/x"])
        text = "\n".join(report)
        self.assertIn("added SOLANA_RPC_URL", text)
        self.assertIn("skipped HELIUS_API_KEY", text)
        self.assertIn("takes precedence over the existing HELIUS_API_KEY", text)

    def test_cli_yes_keeps_an_existing_helius_key_in_charge(self):
        from chaos_trader.cli import main
        self.home.mkdir(parents=True)
        (self.home / ".env").write_text("HELIUS_API_KEY=k\n", encoding="utf-8")
        old = os.environ.pop("SOLANA_RPC_URL", None)
        try:
            import contextlib, io
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(main(["onboard", "--home", str(self.home), "--yes"]), 0)
        finally:
            if old is not None:
                os.environ["SOLANA_RPC_URL"] = old
        self.assertEqual((self.home / ".env").read_text(encoding="utf-8"), "HELIUS_API_KEY=k\n")
        self.assertIn("SOLANA_RPC_URL: skipped (HELIUS_API_KEY already set; the key builds the Helius RPC URL)", out.getvalue())

    def test_cli_yes_adds_the_default_url_when_the_existing_env_has_no_rpc_or_key(self):
        from chaos_trader.cli import main
        self.home.mkdir(parents=True)
        (self.home / ".env").write_text("OTHER=1\n", encoding="utf-8")
        import contextlib, io
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["onboard", "--home", str(self.home), "--yes"]), 0)
        self.assertEqual((self.home / ".env").read_text(encoding="utf-8").splitlines(),
                         ["OTHER=1", "SOLANA_RPC_URL=https://api.mainnet-beta.solana.com"])

    def test_cli_explicit_rpc_url_is_added_even_with_an_existing_key(self):
        from chaos_trader.cli import main
        self.home.mkdir(parents=True)
        (self.home / ".env").write_text("HELIUS_API_KEY=k\n", encoding="utf-8")
        import contextlib, io
        with contextlib.redirect_stdout(io.StringIO()):
            main(["onboard", "--home", str(self.home), "--yes", "--rpc-url", "https://rpc.example/x"])
        self.assertEqual((self.home / ".env").read_text(encoding="utf-8").splitlines(),
                         ["HELIUS_API_KEY=k", "SOLANA_RPC_URL=https://rpc.example/x"])

    def test_existing_env_without_trailing_newline_is_not_corrupted(self):
        self.home.mkdir(parents=True)
        (self.home / ".env").write_text("OTHER=1", encoding="utf-8")
        onboard(self.home, rpc_url="https://rpc.example/x", helius_key=None)
        self.assertEqual((self.home / ".env").read_text(encoding="utf-8"), "OTHER=1\nSOLANA_RPC_URL=https://rpc.example/x\n")

    def test_solana_client_reads_rpc_from_chaos_home_env(self):
        import subprocess, sys
        self.home.mkdir(parents=True)
        (self.home / ".env").write_text("SOLANA_RPC_URL=https://from-env-file.example\n", encoding="utf-8")
        script = Path(__file__).resolve().parents[1] / "skills" / "blockchain" / "solana" / "scripts" / "solana_client.py"
        code = "import runpy,sys; print(runpy.run_path(sys.argv[1], run_name='probe')['RPC_URL'])"
        env = {k: v for k, v in os.environ.items() if k not in ("SOLANA_RPC_URL", "CHAOS_PROFILE_HOME", "HERMES_HOME")}
        env["CHAOS_HOME"] = str(self.home)
        out = subprocess.run([sys.executable, "-c", code, str(script)], env=env, capture_output=True, text=True)
        self.assertEqual(out.stdout.strip(), "https://from-env-file.example", out.stderr)
        (self.home / ".env").write_text("OTHER=1\nexport SOLANA_RPC_URL=https://exported.example\n", encoding="utf-8")
        out = subprocess.run([sys.executable, "-c", code, str(script)], env=env, capture_output=True, text=True)
        self.assertEqual(out.stdout.strip(), "https://exported.example", out.stderr)
        env["SOLANA_RPC_URL"] = "https://process-env.example"
        out = subprocess.run([sys.executable, "-c", code, str(script)], env=env, capture_output=True, text=True)
        self.assertEqual(out.stdout.strip(), "https://process-env.example", out.stderr)


if __name__ == "__main__":
    unittest.main()
