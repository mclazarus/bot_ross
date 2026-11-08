"""Build number and changelog, read from CHANGELOG.md, which ships in the image.

Ported from joshua's release module. The running build is the newest entry in the changelog.
Its commit comes from ``_commit.txt``, which ``git archive`` fills in (see .gitattributes;
home-docks-infra's build-ship.sh builds from ``git archive HEAD``); in a checkout we ask git
instead.

Pure: no Discord, OpenAI, or bot side effects, so it's unit tested in isolation (see
test_release.py). The &version/&changelog/startup-greeting text is built here too, so the
wording is testable without discord.py.
"""

import re
import subprocess
from dataclasses import dataclass
from functools import cache
from pathlib import Path

HERE = Path(__file__).parent
HEADING = re.compile(r"^## Build (\d+)\s*·\s*(\d{4}-\d{2}-\d{2})(?:\s*·\s*([0-9a-f]{7,40}))?\s*$")


@dataclass(frozen=True)
class Release:
    build: int
    date: str
    commit: str | None
    notes: str


def parse_changelog(text):
    """Every ``## Build N · date [· commit]`` entry, newest first."""
    out = []
    head = None
    lines = []

    def close():
        if head:
            out.append(Release(int(head[1]), head[2], head[3], "\n".join(lines).strip()))

    for line in text.splitlines():
        if line.startswith("## "):
            close()
            head, lines = HEADING.match(line), []
        elif head:
            lines.append(line)
    close()
    return sorted(out, key=lambda r: r.build, reverse=True)


@cache
def releases():
    return parse_changelog((HERE / "CHANGELOG.md").read_text(encoding="utf-8"))


def current():
    return releases()[0]


def find(build):
    return next((r for r in releases() if r.build == build), None)


@cache
def commit():
    """The commit this code was built from: stamped by ``git archive``, else from git, else None."""
    try:
        stamped = (HERE / "_commit.txt").read_text().strip()
    except OSError:
        stamped = ""
    if stamped and not stamped.startswith("$Format"):
        return stamped
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=HERE, capture_output=True, text=True, timeout=5
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return (out.stdout.strip() or None) if out.returncode == 0 else None


# --- Discord text --------------------------------------------------------------------

def build_line(r, commit_hash):
    return f"**Build {r.build}** · {r.date}" + (f" · `{commit_hash}`" if commit_hash else "")


def version_message(r, commit_hash):
    return f"Bot Ross {build_line(r, commit_hash or 'dev')}. See `&changelog` for what's new."


def greeting_message(r, commit_hash):
    return (f"A wild Bot Ross appears! {build_line(r, commit_hash or 'dev')}. "
            f"See `&changelog` for any new stuff.")


def unwrap_notes(notes):
    """Join the file's hard-wrapped bullet continuations so Discord shows one line per bullet."""
    lines = []
    for n in notes.splitlines():
        if n.startswith("- "):
            lines.append("• " + n[2:])
        elif n.strip() and lines and n.startswith(" "):
            lines[-1] += " " + n.strip()
        else:
            lines.append(n)
    return "\n".join(lines)


def changelog_message(r, commit_hash, builds):
    text = f"{build_line(r, commit_hash)}\n{unwrap_notes(r.notes)}"
    others = [str(b) for b in builds if b != r.build]
    if others:
        text += "\n-# Earlier builds: `&changelog <build>` · " + ", ".join(others)
    return text


def unknown_build(build, builds):
    return f"No record of build {build}. Builds on file: {', '.join(map(str, builds))}."
