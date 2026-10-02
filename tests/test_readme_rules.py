import re
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BANNED = ["leverage", "seamless", "robust", "powerful", "cutting-edge", "unlock", "empower",
          "elevate", "supercharge", "next-generation", "revolutionize"]
REQUIRED_HEADINGS = ["## 60-second path", "## What it does", "## Install", "## Install for Hermes", "## First run", "## Configuration",
                     "## How it works", "## Skills", "## Use it from an MCP agent", "## Running it on a schedule", "## Keeping it current", "## Execution",
                     "## Security boundary", "## License"]
EMOJI = re.compile("[\U0001F300-\U0001FAFF\u2600-\u27BF]")
# The one statement of what the package recommends; every file below carries it byte for byte.
RECOMMENDATION = ("This package labels tokens study, watch, manual-review, or avoid-entry; its paper loop decides "
                  "enter, wait, or avoid; and for tokens your own wallets hold it reports a position state (hold-core, "
                  "manage, trim-risk, or exit-watch) that describes risk, not an instruction. It never says buy or sell, "
                  "and it cannot sign, route, or send.")
RECOMMENDATION_FILES = ["README.md", "SECURITY.md", "BOUNDARY.md", "SOUL.md", "AGENTS.md",
                        "skills/blockchain/chaos-crypto-trader/SKILL.md",
                        "skills/blockchain/chaos-execution-control/SKILL.md"]
# A trade recommendation in prose: buy/sell, trim, or exit in a sentence that recommends, advises, or gives a
# verdict, a "decisive" verdict, or a backticked buy/sell/hold/trim/exit label. Not a recommendation: code
# (package labels such as the gate and position states `study-caution`, `trim-risk`, and `exit-watch`, or
# `paper_exit`), hyphenated label words, quoted user questions,
# negated sentences ("never a buy/sell instruction"), and "exit code".
TRADE_WORDS = re.compile(r"buy/sell|buy, sell|(?<![\w-])(?:trim|exit)(?![\w-])", re.I)
ADVICE_WORDS = re.compile(r"\b(?:recommend\w*|advice|advis\w*|verdicts?|calls?)\b", re.I)
NEGATION = re.compile(r"\b(?:never|not|no)\b", re.I)
VERDICT_LABEL = re.compile(r"`(?:buy|sell|hold|trim|exit)`", re.I)
GUARDED_FILES = ["README.md", "SECURITY.md", "BOUNDARY.md", "SOUL.md", "AGENTS.md", "CLAUDE.md",
                 *sorted(p.relative_to(ROOT).as_posix() for p in (ROOT / "skills" / "blockchain").glob("*/SKILL.md"))]
SECURITY_HEADINGS = ["# Security", "## Reporting a vulnerability", "## Supported versions", "## What this package stores",
                     "## Execution boundary", "## Forbidden in the repository", "## Release gate"]

# Every way the package reads a named environment variable: os.environ.get/os.getenv/os.environ[...],
# the flag/presence/integer helpers, and the GMGN adapter's copy of the environment (`source.get`).
ENV_READ = re.compile(
    r"""(?:os\.environ\.get|os\.getenv|_flag_enabled|_has_env_key|_int_env|source\.get)\(\s*["']([A-Z][A-Z0-9_]+)["']"""
    r"""|os\.environ\[["']([A-Z][A-Z0-9_]+)["']\]"""
)
# smart_money_signal_client.py builds its two names as f"{_ENV_PREFIX}_BASE" and f"{_ENV_PREFIX}_TOKEN".
ENV_PREFIX = re.compile(r"""_ENV_PREFIX = ["']([A-Z][A-Z0-9_]+)["']""")
ENV_PREFIX_SUFFIX = re.compile(r"\{_ENV_PREFIX\}_([A-Z0-9_]+)")
# Read by the code but not user settings: interpreter and OS plumbing that scripts pass through to
# child processes (PYTHONPATH, PYTHONIOENCODING, HOME, PATH, TMP, TMPDIR, LANG, LC_ALL), and the Windows
# LOCALAPPDATA folder that skills install reads to find the Hermes home.
NOT_USER_SETTINGS = {"PYTHONPATH", "PYTHONIOENCODING", "HOME", "PATH", "TMP", "TMPDIR", "LANG", "LC_ALL", "LOCALAPPDATA"}


def trade_recommendations(text: str) -> list[str]:
    text = text.replace(RECOMMENDATION, "")
    hits = VERDICT_LABEL.findall(text)
    text = re.sub(r"^[ \t]*```.*?^[ \t]*```", "", text, flags=re.M | re.S)
    text = re.sub(r"`[^`\n]*`", "", text)
    text = re.sub(r'"[^"\n]*"', "", text)
    text = re.sub(r"\bexit codes?\b", "", text, flags=re.I)
    hits += re.findall(r"\bdecisive\b", text, re.I)
    for sentence in re.split(r"(?<=[.;!?])\s+|\n", text):
        if TRADE_WORDS.search(sentence) and ADVICE_WORDS.search(sentence) and not NEGATION.search(sentence):
            hits.append(sentence.strip())
    return hits


def env_vars_read_by_code() -> set[str]:
    from chaos_trader.home import ENV_PRECEDENCE  # read in a loop, so the regex cannot see them
    names = set(ENV_PRECEDENCE)
    for path in [*(ROOT / "chaos_trader").rglob("*.py"), *(ROOT / "skills").rglob("*.py")]:
        if path.name.startswith("test_"):
            continue
        text = path.read_text(encoding="utf-8")
        for m in ENV_READ.finditer(text):
            names.add(m.group(1) or m.group(2))
        for prefix in ENV_PREFIX.findall(text):
            names.update(f"{prefix}_{suffix}" for suffix in ENV_PREFIX_SUFFIX.findall(text))
    return names - NOT_USER_SETTINGS


class ReadmeRuleTests(unittest.TestCase):
    def setUp(self):
        self.text = (ROOT / "README.md").read_text(encoding="utf-8")

    def test_no_banned_words(self):
        low = self.text.lower()
        self.assertEqual([w for w in BANNED if re.search(rf"\b{re.escape(w)}", low)], [])

    def test_no_emoji(self):
        self.assertIsNone(EMOJI.search(self.text))

    def test_required_headings_in_order(self):
        positions = [self.text.find(h) for h in REQUIRED_HEADINGS]
        self.assertNotIn(-1, positions, dict(zip(REQUIRED_HEADINGS, positions)))
        self.assertEqual(positions, sorted(positions))

    def test_no_performance_claims(self):
        for phrase in ("win rate", "profit", "roi", "returns", "% gain", "edge"):
            self.assertIsNone(re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", self.text.lower()), phrase)

    def test_any_rpc_matrix_table_exists(self):
        header = re.search(r"^\| Command \| Works on any RPC \| Needs Helius \|$", self.text, re.M)
        self.assertIsNotNone(header, "the any-RPC matrix header row is missing")
        rows = self.text[header.end():].split("\n\n", 1)[0]
        for command in ("`chaos token`", "`chaos analyze token`", "`chaos sweep`", "`chaos sweep --fast`",
                        "`chaos paper-report`", "`chaos wallets`", "`chaos wallets --discover`", "elite ingest job",
                        "paper tick", "`chaos run wallet_deep`"):
            self.assertIn(command, rows)

    def test_any_rpc_matrix_has_the_x_evidence_row(self):
        self.assertIn("| X evidence (`--with-x`) | Yes | No, needs an xAI key |", self.text)

    def test_skills_install_and_home_export_documented(self):
        self.assertIn("chaos skills install", self.text)
        self.assertIn("chaos skills install --for all", self.text)
        self.assertIn('export CHAOS_HOME="$HOME/.chaos-trader"', self.text)
        self.assertIn('$env:CHAOS_HOME = "$HOME\\.chaos-trader"', self.text)

    def test_seven_job_wrappers_named(self):
        wrappers = sorted(p.stem for p in (ROOT / "chaos_trader" / "jobs").glob("chaos_*.py"))
        self.assertEqual(len(wrappers), 7)
        for name in wrappers:
            self.assertIn(f"`{name}`", self.text)

    def test_one_repo_layout(self):
        # Code runs from the installed package through `chaos run`; the profile install is Hermes's own.
        self.assertIn("chaos run ", self.text)
        self.assertIn("hermes profile install", self.text)
        for stale in ("sync_to_profile", "CHAOS_HOME/scripts", "trading/scripts"):
            self.assertNotIn(stale, self.text)

    def test_profile_name_and_install_lines(self):
        # The profile is chaos-trader so an --alias wrapper cannot shadow the chaos command; the PyPI
        # name is unclaimed, so every pip line installs from the repository.
        self.assertIn("hermes -p chaos-trader cron resume", self.text)
        for stale in ("hermes -p chaos ", "profile update chaos ", "pip install chaos-trader", "pip install -U chaos-trader"):
            self.assertNotIn(stale, self.text)

    def test_no_openclaw_claim(self):
        self.assertNotIn("openclaw", self.text.lower())

    def test_every_env_var_documented(self):
        names = env_vars_read_by_code()
        self.assertIn("SOLANA_RPC_URL", names)  # the derivation itself must keep working
        missing = sorted(n for n in names if f"`{n}`" not in self.text)
        self.assertEqual(missing, [])


class SecurityPolicyTests(unittest.TestCase):
    def test_security_headings_in_order(self):
        text = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
        self.assertEqual([line for line in text.splitlines() if line.startswith("#")], SECURITY_HEADINGS)

    def test_make_verify_sentence_matches_the_makefile(self):
        # SECURITY.md names every target of `make verify`, in the Makefile's order.
        phrases = {"safety-check": "the safety check", "vendor-check": "the vendor check", "compile": "a compile pass",
                   "test": "the tests", "gitleaks-scan": "the gitleaks scan",
                   "history-scan": "the full-history private-name scan", "dep-audit": "the dependency audit"}
        makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
        targets = re.search(r"^verify:(.*)$", makefile, re.M).group(1).split()
        sentence = re.search(r"^`make verify` runs .*$", (ROOT / "SECURITY.md").read_text(encoding="utf-8"), re.M).group(0)
        self.assertEqual(targets, list(phrases), "a new verify target needs a phrase here and in SECURITY.md")
        self.assertIn("runs seven gates in this order", sentence)
        positions = [sentence.find(phrases[t]) for t in targets]
        self.assertNotIn(-1, positions, dict(zip(targets, positions)))
        self.assertEqual(positions, sorted(positions))
        # A leading - on a recipe line would let that gate fail without failing `make verify`.
        for target in targets:
            self.assertRegex(makefile, rf"(?m)^{re.escape(target)}:\n\t[^-]", target)

    def test_security_boundary_and_agent_files_have_no_banned_words(self):
        for name in ("SECURITY.md", "BOUNDARY.md", "AGENTS.md", "CLAUDE.md"):
            low = (ROOT / name).read_text(encoding="utf-8").lower()
            self.assertEqual([w for w in BANNED if re.search(rf"\b{re.escape(w)}", low)], [], name)

    def test_one_recommendation_sentence_everywhere(self):
        for name in RECOMMENDATION_FILES:
            self.assertIn(RECOMMENDATION, (ROOT / name).read_text(encoding="utf-8"), name)

    def test_no_trade_recommendation_outside_the_one_sentence(self):
        self.assertGreater(len(GUARDED_FILES), 12)
        hits = {name: trade_recommendations((ROOT / name).read_text(encoding="utf-8")) for name in GUARDED_FILES}
        self.assertEqual({name: found for name, found in hits.items() if found}, {})

    def test_gate_and_position_states_in_backticks_are_labels(self):
        # Every entry-gate and position state the card prints may sit in backticks beside "verdict";
        # the bare word in the same sentence is still a recommendation.
        from chaos_trader.trading.scripts.position_context import ALLOWED_ENTRY_ACTIONS, ALLOWED_POSITION_ACTIONS
        for label in sorted(ALLOWED_ENTRY_ACTIONS | ALLOWED_POSITION_ACTIONS):
            self.assertEqual(trade_recommendations(f"The card's verdict on this holding is `{label}`."), [], label)
        bare = "The card's verdict on this holding is trim."
        self.assertEqual(trade_recommendations(bare), [bare])


class SmartWalletPageTests(unittest.TestCase):
    LINK = "The wallet lane, from the seed roster to the paper book, is documented in `docs/smart-wallets.md`."

    def test_page_and_svg_text_have_no_banned_words(self):
        page = (ROOT / "docs" / "smart-wallets.md").read_text(encoding="utf-8")
        svg = ET.parse(ROOT / "docs" / "smart-wallets.svg").getroot()
        labels = " ".join("".join(el.itertext()) for el in svg.iter() if el.tag.rsplit("}", 1)[-1] == "text")
        self.assertTrue(labels.strip(), "the SVG has no text nodes")
        for name, text in (("docs/smart-wallets.md", page), ("docs/smart-wallets.svg", labels)):
            low = text.lower()
            self.assertEqual([w for w in BANNED if re.search(rf"\b{re.escape(w)}", low)], [], name)

    def test_readme_links_the_page(self):
        self.assertIn(self.LINK, (ROOT / "README.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
