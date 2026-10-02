"""Fails if a vendor or owner name re-enters the public tree. CI runs this on every push.

The banned tokens are stored only as sha256 digests of their lowercase form, so this file
never publishes the identifiers it guards. Failures name the file and what kind of
identifier was found, never the token itself.
"""
import hashlib
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# sha256(token.lower()) -> what the token is. Only the digests live here.
FORBIDDEN_TOKEN_DIGESTS = {
    "160a8c6dc4ece86f95e1b4ef96e90f61a817f569865ab1e6d52ab0b9ce5f5c35": "private host address",
    "08fb439b99c1ab58b618a57b668edb6574fd1789482e9dfc90580010928a0dc8": "private host name",
    "26c894e7fd43a90d6406e6026699d60366e5d7ee107ab6d836cf9e324a525cbe": "private network domain",
    "809cd9315d98c74bc8c3e8d607339dc831834a9e3ffac39f4a77776ec556e91d": "private ssh alias",
    "d9cd77a0e4825010244a6ae67d62accbad670c8afc42813458b307f262dea731": "private ssh key name",
    "a8f0d90a0f03ae139720545019e9165470906e774db87332464c553d1e13270b": "private backup bucket",
    "237ce2026b44e2a33c9b148500fe5c474874295d7f5ad3e8ceb037ee40077afa": "private backup bucket",
    "54fea87e492b766ec59f4dbfb1f6eaec7d6152884e1a71ebfa147172250374ff": "competitor name",
    "ead6ef03d61ee60c533d6d450c50a1e559a8a37f6b796a4094cd0dac6b744428": "owner first name",
    "bac97720b90f6cd0d3589ec212f68bcfe26fbbe72425d9295fd3576ccd201085": "owner email local part",
    "ef4bb6d983dc0fd1487bd20aee24b1f2a00e3ee49768adce485af9f4278a874c": "windows username",
    "5667ef73dac709effcfdc518a98ea9238365ec62b0eb5f478392e28818a2efd5": "worker-profile name",
    "598f7a741a1e3a05654d346033571fda567af6dc2bf099b34b930171519d995f": "worker-profile name",
    "4e8ff5bd7ff87fe64023989bffa734211cbe7e84488092fd621d5750e89b68c8": "worker-profile name",
    "7c82602500857aa6ed0cf38c4c3e4ec645bdcaa82c00b9155eb08be100c778a9": "worker-profile name",
    "12c87370d1b5472793e67682596b60efe2c6038d63d04134a1a88544509737b4": "private-host install path",
    "03fbce00745f7e7e857117fb16da743d8241fa6693d5be9e6533a27c64732b9f": "dated private artifact name",
    "6da38f17cb82192eb241bd0c8912970423418a4ad329f3d3aa49e1e7e49196bc": "chat-bot wording",
    "49fe35138d82eaac2cc8b11ef023b84d888aa9d55e5a4cf8002252510b159e9c": "external signal product name",
}
# sha256 of a lowercase phrase of 2 or 3 consecutive tokens, joined by one space.
FORBIDDEN_PHRASE_DIGESTS = {
    "a9581c0c1a087ebd167a2cb87ecea2c1de212417d2e744a5ce39a725ed52fd0c": "private hermes profile path",
    "380a786f724233996618e93e1efec4db62877741f001b5856d0987f43629c2a9": "private hermes profile path",
    "5ccc0a0a70d8e83e582e666376172b9c28229e363895b607d309c6d787c87e49": "private profile setting",
    "408eb7564edad43ea1f0c31fc0088ec4e7950faec5694c0346a46570e05175a2": "chat-bot wording",
    "710df17f5a9153e2629e91ed469c96388f5deb15cbead4aa921260ef09f014ba": "chat-bot wording",
}
# File-name prefixes (the part before the first "_") that are banned, also as digests.
FORBIDDEN_PREFIX_DIGESTS = {
    "54fea87e492b766ec59f4dbfb1f6eaec7d6152884e1a71ebfa147172250374ff": "competitor name",
    "ead6ef03d61ee60c533d6d450c50a1e559a8a37f6b796a4094cd0dac6b744428": "owner first name",
}
FORBIDDEN_PATHS = [
    re.compile(r"(^|/)owner_"),
    re.compile(r"(^|/)trading/plans/"),
    re.compile(r"(^|/)experimental/"),
    re.compile(r"\.sqlite$|\.db$|(^|/)\.env$|(^|/)auth\.json$"),
]
TOKEN = re.compile(r"[A-Za-z0-9_@.'-]+")
SUBPART = re.compile(r"[A-Za-z0-9]+")


def digest(token: str) -> str:
    return hashlib.sha256(token.lower().encode()).hexdigest()


def banned_kinds(text: str) -> set[str]:
    """Kinds of banned token found in text. Each token is checked whole and split into
    its alphanumeric parts, so possessives, domains and email addresses are caught too."""
    kinds = set()
    for token in set(TOKEN.findall(text)):
        for part in {token, *SUBPART.findall(token), *re.split(r"[@./:]+", token)}:
            kind = FORBIDDEN_TOKEN_DIGESTS.get(digest(part))
            if kind:
                kinds.add(kind)
    words = [w.lower() for w in TOKEN.findall(text)]
    for size in (2, 3):
        for i in range(len(words) - size + 1):
            kind = FORBIDDEN_PHRASE_DIGESTS.get(hashlib.sha256(" ".join(words[i:i + size]).encode()).hexdigest())
            if kind:
                kinds.add(kind)
    return kinds


def tracked_files() -> list[str]:
    out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    return [line for line in out.splitlines() if line]


class NoVendorNamesTests(unittest.TestCase):
    def test_no_forbidden_paths(self):
        bad = []
        for f in tracked_files():
            if any(p.search(f) for p in FORBIDDEN_PATHS):
                bad.append(f)
                continue
            for segment in f.split("/"):
                kind = FORBIDDEN_PREFIX_DIGESTS.get(digest(segment.split("_", 1)[0])) if "_" in segment else None
                if kind:
                    bad.append(f"{f}: file name starts with a {kind}")
                    break
        self.assertEqual(bad, [])

    def test_no_forbidden_text(self):
        hits = []
        for f in tracked_files():
            try:
                text = (ROOT / f).read_text(encoding="utf-8")
            except (UnicodeDecodeError, FileNotFoundError):
                continue
            for kind in sorted(banned_kinds(text)):
                hits.append(f"{f}: {kind}")
        self.assertEqual(hits, [])


if __name__ == "__main__":
    unittest.main()
