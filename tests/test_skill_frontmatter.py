"""Every shipped SKILL.md loads in Claude Code, Codex, and Hermes, and carries no private-tooling residue.

All three harnesses need YAML frontmatter on line 1 with `name` (64 chars or fewer) and
`description` (1024 chars or fewer). The residue words are stored as sha256 digests of their
lowercase, space-joined word form, like tests/test_no_vendor_names.py, so this file never
publishes them.
"""
import hashlib
import re
import unittest
from pathlib import Path

import yaml

SKILLS = Path(__file__).resolve().parents[1] / "skills" / "blockchain"

# sha256 of the lowercase words, space-joined -> what the residue is.
RESIDUE_DIGESTS = {
    "5667ef73dac709effcfdc518a98ea9238365ec62b0eb5f478392e28818a2efd5": "worker-profile name",
    "598f7a741a1e3a05654d346033571fda567af6dc2bf099b34b930171519d995f": "worker-profile name",
    "4e8ff5bd7ff87fe64023989bffa734211cbe7e84488092fd621d5750e89b68c8": "worker-profile name",
    "7c82602500857aa6ed0cf38c4c3e4ec645bdcaa82c00b9155eb08be100c778a9": "worker-profile name",
    "49fe35138d82eaac2cc8b11ef023b84d888aa9d55e5a4cf8002252510b159e9c": "signal product name",
    "3f40462915a3e6026a4d790127b95ded4d870f6ab18d9af2fcbc454168255237": "chat app",
    "27c3158e34c1e422ea015830b9cbeaf865109a8d0544332ae4b87d96e2a38b80": "out-of-scope venue",
    "710df17f5a9153e2629e91ed469c96388f5deb15cbead4aa921260ef09f014ba": "chat command wording",
    "f3853ec422d1b892a86aa9113d78800ef32a5e40e0bd25012a4a02b820c235a7": "chat command wording",
    "a00e17cfc9b5911ebc201ac29da6231228d8af0f63da4c9f60a47aba220c5a52": "Mac-only path",
    "5ccc0a0a70d8e83e582e666376172b9c28229e363895b607d309c6d787c87e49": "owner profile key",
    "3a83c77f6d33c3db7030987141c19fc96b166995d52401e84eed17ee3c79731a": "owner artifact date",
}
WORD = re.compile(r"[a-z0-9]+")
PREREQUISITES = (
    "## Prerequisites\n\n"
    "- The package is installed: `pip install git+https://github.com/AIEngineerX/chaos-trader`"
    " (or it came with the Hermes profile).\n"
    "- A home exists: `chaos onboard` was run once (`chaos onboard --yes` for the defaults).\n"
    "- `CHAOS_HOME` points at that home, or you are inside the Hermes profile, which sets it."
    " Check with `chaos --version` and `chaos help`.\n"
)
# Operator-persona residue. Checked in prose only: code spans, fenced blocks, and H1 titles are dropped,
# so field names such as `owner_exposure` and skill titles such as "Chaos Crypto Trader" stay.
PERSONA = {
    "operator": re.compile(r"\boperators?\b", re.I),
    "owner": re.compile(r"\bowners?\b", re.I),
    "Phase 1": re.compile(r"\bphase 1\b", re.I),
    "Chaos as an actor": re.compile(r"\bChaos\b"),
}
FENCE = re.compile(r"^[ \t]*```.*?^[ \t]*```", re.M | re.S)
CODE_SPAN = re.compile(r"`[^`\n]*`")
CRYPTO_TRADER_TRIGGERS = ["what does the read say about this token", "what would the paper loop do with this token"]
POSITION_TRIGGERS = ["verdict with the tokens my own wallets hold in mind",
                     "should I add to or exit a position my wallets hold"]


def residue_kinds(text: str) -> set[str]:
    words = WORD.findall(text.lower())
    grams = set(words)
    grams |= {" ".join(words[i:i + 2]) for i in range(len(words) - 1)}
    grams |= {" ".join(words[i:i + 3]) for i in range(len(words) - 2)}
    kinds = set()
    for gram in grams:
        kind = RESIDUE_DIGESTS.get(hashlib.sha256(gram.encode()).hexdigest())
        if kind:
            kinds.add(kind)
    return kinds


def prose(text: str) -> str:
    text = CODE_SPAN.sub("", FENCE.sub("", text))
    return "\n".join(line for line in text.splitlines() if not line.startswith("# "))


def when_to_use_bullets(text: str) -> list[str]:
    section = re.search(r"^## When to Use\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    if not section:
        return []
    return [line.strip() for line in section.group(1).splitlines() if line.strip().startswith("- ")]


def frontmatter(text: str) -> dict:
    lines = text.splitlines()
    end = lines.index("---", 1)
    return yaml.safe_load("\n".join(lines[1:end]))


class SkillFrontmatterTests(unittest.TestCase):
    def skill_files(self) -> list[Path]:
        files = sorted(SKILLS.glob("*/SKILL.md"))
        self.assertEqual(len(files), 14)
        return files

    def test_frontmatter_starts_on_line_one(self):
        for f in self.skill_files():
            with self.subTest(skill=f.parent.name):
                self.assertEqual(f.read_text(encoding="utf-8").splitlines()[0], "---")

    def test_name_matches_folder_and_fits(self):
        for f in self.skill_files():
            with self.subTest(skill=f.parent.name):
                meta = frontmatter(f.read_text(encoding="utf-8"))
                self.assertEqual(meta["name"], f.parent.name)
                self.assertLessEqual(len(meta["name"]), 64)

    def test_description_present_and_fits(self):
        for f in self.skill_files():
            with self.subTest(skill=f.parent.name):
                description = frontmatter(f.read_text(encoding="utf-8")).get("description")
                self.assertIsInstance(description, str)
                self.assertTrue(description.strip())
                self.assertLessEqual(len(description), 1024)

    def test_no_residue_words(self):
        hits = []
        for f in self.skill_files():
            for kind in sorted(residue_kinds(f.read_text(encoding="utf-8"))):
                hits.append(f"{f.parent.name}: {kind}")
        self.assertEqual(hits, [])

    def test_skills_call_chaos_verbs_only(self):
        banned = ["${", "$HOME", "$CHAOS_HOME", "trading/scripts", "/scripts/chaos_",
                  "python3 ", 'python "', "python '", '.py"']
        root = SKILLS.parents[1]
        script_dirs = [root / "chaos_trader" / "trading" / "scripts", root / "chaos_trader" / "jobs",
                       *SKILLS.glob("*/scripts")]
        hits = []
        for f in self.skill_files():
            text = f.read_text(encoding="utf-8")
            hits += [f"{f.parent.name}: {s!r}" for s in banned if s in text]
            for name in re.findall(r"chaos run (\w+)", text):
                if not any((d / f"{name}.py").is_file() for d in script_dirs):
                    hits.append(f"{f.parent.name}: chaos run {name} has no script")
        self.assertEqual(hits, [])

    def test_every_skill_states_its_prerequisites(self):
        for f in self.skill_files():
            with self.subTest(skill=f.parent.name):
                self.assertIn(PREREQUISITES, f.read_text(encoding="utf-8"))

    def test_no_operator_persona_wording(self):
        hits = []
        for f in self.skill_files():
            text = prose(f.read_text(encoding="utf-8"))
            for label, pattern in PERSONA.items():
                hits += [f"{f.parent.name}: {label}: {m.group(0)!r}" for m in pattern.finditer(text)]
        self.assertEqual(hits, [])

    def test_trigger_phrases_are_distinct(self):
        texts = {f.parent.name: f.read_text(encoding="utf-8") for f in self.skill_files()}
        skills_with = {}
        for name, text in texts.items():
            quoted = re.findall(r'"([^"]+)"', frontmatter(text)["description"])
            for phrase in {*quoted, *when_to_use_bullets(text)}:
                skills_with.setdefault(phrase.lower(), set()).add(name)
        shared = {phrase: sorted(names) for phrase, names in skills_with.items() if len(names) > 1}
        self.assertEqual(shared, {})
        for phrase in CRYPTO_TRADER_TRIGGERS:
            self.assertEqual(sorted(skills_with.get(phrase, set())), ["chaos-crypto-trader"], phrase)
        self.assertNotIn("should i buy this", skills_with)
        for phrase in POSITION_TRIGGERS:
            self.assertEqual(sorted(skills_with.get(phrase.lower(), set())), ["chaos-position-aware-alpha"], phrase)


if __name__ == "__main__":
    unittest.main()
