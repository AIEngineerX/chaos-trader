import asyncio
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
except ImportError:
    ClientSession = None

ROOT = Path(__file__).resolve().parents[1]
PUBLIC_RPC = "https://api.mainnet-beta.solana.com"
TOOLS = ["analyze_token", "paper_report", "roster_list", "strategy_paper", "sweep", "token_read", "wallets_review"]
MCP_EXTRA = 'chaos mcp needs the MCP extra: pip install "chaos-trader[mcp] @ git+https://github.com/AIEngineerX/chaos-trader"'
NO_INGEST_REVIEW = "No ingest yet. Run chaos run chaos_alpha_elite_ingest first."
NOT_A_MINT = "not a Solana mint address"
SEED = json.loads((ROOT / "chaos_trader" / "seed" / "roster.json").read_text(encoding="utf-8"))
DROP = ("HELIUS_API_KEY", "SOLANA_RPC_URL", "CHAOS_PROFILE_HOME", "HERMES_HOME", "XAI_API_KEY", "X_SEARCH_PROVIDER", "HERMES_AGENT_SRC")


def files_under(root: Path, skip: Path | None = None) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if skip is None or (p != skip and skip not in p.parents))


class McpExtraMissingTests(unittest.TestCase):
    def test_chaos_mcp_without_the_extra_exits_2_with_the_install_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {k: v for k, v in os.environ.items() if k not in DROP}
            env.update(CHAOS_HOME=str(Path(tmp) / "home"), HOME=tmp, USERPROFILE=tmp)
            code = "import sys; sys.modules['mcp'] = None; from chaos_trader.cli import main; raise SystemExit(main(['mcp']))"
            p = subprocess.run([sys.executable, "-c", code], stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8", env=env)
            self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
            self.assertEqual(p.stderr, MCP_EXTRA + "\n")
            self.assertEqual(p.stdout, "")


class OnboardedHome(unittest.TestCase):
    """A temp home made by `chaos onboard`, HOME redirected, every command run from the temp folder."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.home = self.tmp / "home"
        user = self.tmp / "user"
        user.mkdir()
        self.env = {k: v for k, v in os.environ.items() if k not in DROP}
        self.env.update(CHAOS_HOME=str(self.home), SOLANA_RPC_URL=PUBLIC_RPC, PYTHONIOENCODING="utf-8", HOME=str(user), USERPROFILE=str(user))
        p = self.run_chaos("onboard", "--rpc-url", PUBLIC_RPC, "--yes")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def run_chaos(self, *args, env=None):
        return subprocess.run([sys.executable, "-m", "chaos_trader.cli", *args], stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, encoding="utf-8", env=env or self.env, cwd=self.tmp, timeout=120)

    def check_envelope(self, text, command):
        out = json.loads(text)
        self.assertEqual(list(out), ["schema_version", "command", "generated_at", "data"])
        self.assertEqual(out["schema_version"], "1")
        self.assertEqual(out["command"], command)
        return out

    def check_seed_roster(self, data):
        self.assertEqual(list(data), ["version", "captured_at", "source", "wallets"])
        self.assertEqual(data["version"], "seed-v2")
        self.assertEqual(len(data["wallets"]), 9)
        self.assertEqual(data["wallets"], SEED["wallets"])


class WalletsListTests(OnboardedHome):
    def test_list_prints_tier_and_address_for_the_nine_seed_wallets(self):
        before = files_under(self.tmp)
        p = self.run_chaos("wallets", "--list")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        lines = p.stdout.splitlines()
        self.assertEqual(lines[:-1], [f"{w['tier']} {w['address']}" for w in SEED["wallets"]])
        self.assertEqual(lines[-1], "9 wallets in the roster (seed-v2, source public)")
        self.assertEqual(files_under(self.tmp), before)

    def test_list_json_is_the_envelope_around_the_roster_file(self):
        p = self.run_chaos("wallets", "--list", "--json")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.check_seed_roster(self.check_envelope(p.stdout, "wallets --list")["data"])

    def test_list_does_not_combine_with_review_or_tier(self):
        for args in (("--list", "--review"), ("--list", "--tier", "A")):
            p = self.run_chaos("wallets", *args)
            self.assertEqual(p.returncode, 2, p.stdout + p.stderr)


@unittest.skipIf(ClientSession is None, 'the MCP SDK is not installed; pip install "mcp>=1.12,<2" to run the server tests')
class McpServerTests(OnboardedHome):
    """`chaos mcp` as a real subprocess, driven by the SDK's stdio client."""

    def session(self, *calls):
        """Run the calls in one server session: None lists the tools, (name, arguments) calls one."""
        async def go():
            params = StdioServerParameters(command=sys.executable, args=["-m", "chaos_trader.cli", "mcp"], env=self.env, cwd=str(self.tmp))
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    return [await (session.list_tools() if call is None else session.call_tool(call[0], arguments=call[1])) for call in calls]
        return asyncio.run(go())

    def envelope(self, result, command):
        self.assertFalse(result.isError, result.content)
        return self.check_envelope(result.content[0].text, command)

    def test_lists_exactly_the_seven_tools_each_with_a_one_sentence_description(self):
        (listed,) = self.session(None)
        self.assertEqual(sorted(t.name for t in listed.tools), TOOLS)
        for tool in listed.tools:
            self.assertRegex(tool.description, r"^[A-Z][^\n]*\.$", tool.name)
            self.assertEqual(tool.description.count(". "), 0, tool.name)

    def test_roster_list_returns_the_nine_seed_wallets(self):
        (result,) = self.session(("roster_list", {}))
        self.check_seed_roster(self.envelope(result, "wallets --list")["data"])

    def test_wallets_review_on_a_fresh_home_is_the_no_ingest_envelope(self):
        (result,) = self.session(("wallets_review", {}))
        out = self.envelope(result, "wallets --review")
        self.assertEqual(out["data"], {"status": "no-ingest", "message": NO_INGEST_REVIEW})

    def test_a_malformed_mint_is_a_tool_error_and_the_server_keeps_answering(self):
        bad, after = self.session(("token_read", {"mint": "not-a-mint"}), ("roster_list", {}))
        self.assertTrue(bad.isError)
        self.assertIn(NOT_A_MINT, bad.content[0].text)
        self.envelope(after, "wallets --list")

    def test_nothing_is_written_outside_the_home(self):
        before = files_under(self.tmp, self.home)
        results = self.session(None, ("roster_list", {}), ("wallets_review", {"days": 7}), ("paper_report", {}))
        for result in results[1:]:
            self.assertFalse(result.isError, result.content)
        self.assertEqual(files_under(self.tmp, self.home), before)

    def test_chaos_mcp_on_a_home_that_is_not_onboarded_exits_2_with_the_onboard_sentence(self):
        nope = self.tmp / "nope"
        p = self.run_chaos("mcp", env=dict(self.env, CHAOS_HOME=str(nope)))
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertEqual(p.stderr, f"No chaos-trader home at {nope.resolve()}. Run `chaos onboard` first (or set CHAOS_HOME to an existing home).\n")
        self.assertFalse(nope.exists())

    def test_the_mint_check_is_the_pipeline_pattern(self):
        from chaos_trader.mcp_server import MINT_RE
        source = (ROOT / "chaos_trader" / "trading" / "scripts" / "chaos_cmd.py").read_text(encoding="utf-8")
        self.assertEqual(re.search(r'^MINT_RE = re\.compile\(r"(.+)"\)$', source, re.M).group(1), MINT_RE.pattern)


if __name__ == "__main__":
    unittest.main()
