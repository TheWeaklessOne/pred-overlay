"""Fail when a tracked file holds something that must not be published.

Run by CI and by the git hooks (.githooks). It looks for what has slipped
into this kind of repository before or could: a path from the author's PC, a
token, an e-mail address, a call to Omeda's own backend (which Omeda asked
projects not to make; see README, Caveats). It cannot know other players' names:
those stay out of the repository by keeping fixtures invented (Test_Name42,
PREDOVERLAY7) and never committing recorded matches or captures.

    python tools/check_public.py            # every tracked file
    python tools/check_public.py FILE...    # just these (the hook passes staged files)
"""

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# This file and its tests hold the patterns themselves.
EXEMPT = {Path(__file__).resolve(), ROOT / "tests" / "test_check_public.py"}

RULES = [
    (
        "a Windows user folder",
        re.compile(rb"[A-Za-z]:[\\/]+Users[\\/]+(?!Public\b)[^\\/\s\"'<>]+", re.I),
    ),
    ("a macOS or Linux home folder", re.compile(rb"/(?:Users|home)/[a-z0-9_.-]+/", re.I)),
    ("a PC name", re.compile(rb"\bDESKTOP-[A-Z0-9]{7}\b")),
    ("Omeda's backend", re.compile(rb"pragmaengine", re.I)),
    ("a JSON Web Token", re.compile(rb"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.")),
    ("a bearer token", re.compile(rb"Bearer [A-Za-z0-9._~+/-]{20,}")),
    ("a client secret", re.compile(rb"client_secret[\"']?\s*[:=]\s*[\"'][^\"']{8,}", re.I)),
    ("a private key", re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("an e-mail address", re.compile(rb"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
]
# Addresses that are meant to be there: GitHub's no-reply ones, and examples.
ALLOWED_MAIL = re.compile(
    rb"@(users\.noreply\.github\.com|anthropic\.com|example\.(com|org))$", re.I
)
# A made-up example that only looks like one (a sample path, a test token) carries
# this marker on its line.
ALLOW = b"public-ok"
TEXT = {
    ".py",
    ".md",
    ".txt",
    ".toml",
    ".yml",
    ".yaml",
    ".json",
    ".spec",
    ".ps1",
    ".cfg",
    ".ini",
    "",
}


def tracked():
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True
    ).stdout
    return [ROOT / name for name in out.decode("utf-8").split("\0") if name]


def problems(path):
    if path.resolve() in EXEMPT or path.suffix.lower() not in TEXT or not path.is_file():
        return
    data = path.read_bytes()
    for line_no, line in enumerate(data.splitlines(), 1):
        if ALLOW in line:
            continue
        for what, rule in RULES:
            for match in rule.finditer(line):
                if what == "an e-mail address" and ALLOWED_MAIL.search(match.group()):
                    continue
                yield line_no, what


def main(argv):
    paths = [Path(a).resolve() for a in argv] if argv else tracked()
    found = 0
    for path in paths:
        for line_no, what in problems(path):
            found += 1
            shown = path.relative_to(ROOT) if ROOT in path.parents else path
            print(f"{shown}:{line_no}: {what}")
    if found:
        print(f"\n{found} problem(s): nothing like this may be published.", file=sys.stderr)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
