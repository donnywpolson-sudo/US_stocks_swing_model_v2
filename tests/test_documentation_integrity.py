from __future__ import annotations

from pathlib import Path
import re
import subprocess
from urllib.parse import unquote


ROOT = Path(__file__).resolve().parents[1]
MARKDOWN_LINK = re.compile(r"!?\[[^]]*\]\(([^)]+)\)")
NON_LOCAL_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*:", re.IGNORECASE)
GENERATED_PREFIXES = ("artifacts/", "data/", "reports/generated/")


def _tracked_markdown_files() -> tuple[Path, ...]:
    result = subprocess.run(
        ("git", "ls-files", "*.md"),
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return tuple(
        ROOT / line
        for line in result.stdout.splitlines()
        if line and not line.startswith(GENERATED_PREFIXES)
    )


def _anchors(path: Path) -> set[str]:
    anchors: set[str] = set()
    counts: dict[str, int] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^#{1,6}\s+(.+?)\s*#*\s*$", line)
        if not match:
            continue
        heading = re.sub(r"\[([^]]+)\]\([^)]+\)", r"\1", match.group(1))
        heading = re.sub(r"[`*_~]", "", heading.casefold())
        slug = re.sub(r"[^\w\- ]", "", heading)
        slug = re.sub(r"[ -]+", "-", slug.strip())
        duplicate = counts.get(slug, 0)
        counts[slug] = duplicate + 1
        anchors.add(slug if duplicate == 0 else f"{slug}-{duplicate}")
    return anchors


def test_tracked_markdown_links_and_anchors_resolve() -> None:
    violations: list[str] = []
    for document in _tracked_markdown_files():
        source = document.read_text(encoding="utf-8")
        for match in MARKDOWN_LINK.finditer(source):
            raw_target = match.group(1).strip()
            if raw_target.startswith("<") and raw_target.endswith(">"):
                raw_target = raw_target[1:-1]
            else:
                raw_target = raw_target.split(maxsplit=1)[0]
            if raw_target.startswith("//") or NON_LOCAL_SCHEME.match(raw_target):
                continue
            path_text, separator, anchor = raw_target.partition("#")
            path_text = unquote(path_text.split("?", maxsplit=1)[0])
            target = document if not path_text else document.parent / path_text
            target = target.resolve()
            try:
                relative_target = target.relative_to(ROOT).as_posix()
            except ValueError:
                violations.append(f"{document.relative_to(ROOT)}: escapes repository: {raw_target}")
                continue
            if not target.is_file():
                violations.append(f"{document.relative_to(ROOT)}: missing {relative_target}")
                continue
            if separator and target.suffix.casefold() == ".md":
                normalized_anchor = unquote(anchor).casefold()
                if normalized_anchor not in _anchors(target):
                    violations.append(
                        f"{document.relative_to(ROOT)}: missing anchor "
                        f"{relative_target}#{normalized_anchor}"
                    )
    assert violations == []
