#!/usr/bin/env python3
"""Read-only drift checker for the sports-picks script copies.

The canonical source of every executable script in this project is
``scripts/`` on ``origin/main`` of this repo. Every other copy on any machine
is derived from it (see ``docs/script-provenance.md``). This checker compares
derived copies back to the canonical tree and reports drift. It never writes,
never repairs, and never touches live runtime state.

Two families of checks run:

*Repo-internal* (always, no arguments needed)
  - every file in the deploy script's ``PROFILE_MANIFEST`` exists in
    ``scripts/`` as a regular file;
  - each vendored duplicate (currently ``skills/sports-picks/scripts/
    http_util.py``) is byte-identical to the canonical file it copies.

*Derived copies* (one ``--copy`` per path you can reach)
  - ``full``      every canonical ``scripts/`` file must be present and
                  byte-identical; used for whole-checkout copies.
  - ``manifest``  only the ``PROFILE_MANIFEST`` subset must match; used for
                  the Hermes profile-local script directory.

Extra files that the canonical tree does not define are drift in ``full`` mode
(``unexpected``) and informational in ``manifest`` mode (``unmanaged``, drift
only under ``--strict``) — see ``compare()`` for why the modes differ.

By default the canonical tree is the working tree of ``--repo-root``. Pass
``--ref origin/main`` to compare against a committed tree instead, which is
what the canonical-source decision actually names — a checkout parked on a
feature branch would otherwise report its own uncommitted work as everyone
else's drift.

Exit codes: 0 clean, 1 drift found, 2 usage or I/O error.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

# Vendored duplicates: (copy, canonical source). Both paths are repo-relative.
# A vendored copy exists so a skill directory is self-contained; nothing else
# enforces the identity, so this checker does.
VENDORED: tuple[tuple[str, str], ...] = (
    ("skills/sports-picks/scripts/http_util.py", "scripts/http_util.py"),
)

SCRIPTS_DIR = "scripts"
DEPLOY_SCRIPT = f"{SCRIPTS_DIR}/deploy-runtime.sh"

COPY_MODES = ("full", "manifest")

# Never compared: build artefacts, not source.
IGNORED_NAMES = {"__pycache__", ".DS_Store"}


class ProvenanceError(Exception):
    """Canonical tree could not be read — a usage/environment failure."""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


class Canonical:
    """The canonical ``scripts/`` tree, read from a working tree or a git ref."""

    def __init__(self, repo_root: Path, ref: str | None = None) -> None:
        self.repo_root = repo_root
        self.ref = ref

    def _git(self, *args: str) -> bytes:
        proc = subprocess.run(
            ["git", "-C", str(self.repo_root), *args],
            capture_output=True,
            check=False,
        )
        if proc.returncode != 0:
            raise ProvenanceError(
                f"git {' '.join(args)} failed: {proc.stderr.decode(errors='replace').strip()}"
            )
        return proc.stdout

    def read_bytes(self, rel: str) -> bytes | None:
        """Contents of a repo-relative path, or None when it does not exist."""
        if self.ref is None:
            path = self.repo_root / rel
            if not path.is_file() or path.is_symlink():
                return None
            return path.read_bytes()
        try:
            return self._git("cat-file", "blob", f"{self.ref}:{rel}")
        except ProvenanceError:
            return None

    def digest(self, rel: str) -> str | None:
        data = self.read_bytes(rel)
        return None if data is None else sha256_bytes(data)

    def scripts(self) -> dict[str, str]:
        """Map of ``scripts/`` file name -> sha256 of its canonical content."""
        if self.ref is None:
            scripts_dir = self.repo_root / SCRIPTS_DIR
            if not scripts_dir.is_dir():
                raise ProvenanceError(f"no {SCRIPTS_DIR}/ directory under {self.repo_root}")
            return {
                p.name: sha256_file(p)
                for p in sorted(scripts_dir.iterdir())
                if p.is_file() and not p.is_symlink() and p.name not in IGNORED_NAMES
            }
        out = self._git("ls-tree", "-r", "--name-only", "-z", self.ref, f"{SCRIPTS_DIR}/")
        names = [n.decode() for n in out.split(b"\0") if n]
        digests: dict[str, str] = {}
        for rel in sorted(names):
            name = rel.split("/")[-1]
            if "/" in rel[len(SCRIPTS_DIR) + 1:] or name in IGNORED_NAMES:
                continue  # scripts/ is flat; nested paths are not part of a copy
            data = self.read_bytes(rel)
            if data is not None:
                digests[name] = sha256_bytes(data)
        if not digests:
            raise ProvenanceError(f"{self.ref}:{SCRIPTS_DIR}/ contains no files")
        return digests

    def label(self) -> str:
        return f"{self.repo_root}@{self.ref}" if self.ref else f"{self.repo_root} (working tree)"


def parse_profile_manifest(deploy_text: str) -> list[str]:
    """Read PROFILE_MANIFEST straight out of deploy-runtime.sh.

    The deploy script is the single definition of which files the profile
    copies contain. Restating that list here would create a second manifest
    free to drift from the one that actually ships files.
    """
    match = re.search(r"^PROFILE_MANIFEST=\(\n(.*?)^\)$", deploy_text, re.MULTILINE | re.DOTALL)
    if not match:
        raise ProvenanceError(f"PROFILE_MANIFEST array not found in {DEPLOY_SCRIPT}")
    names: list[str] = []
    for raw in match.group(1).splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            names.extend(line.split())
    if not names:
        raise ProvenanceError(f"PROFILE_MANIFEST in {DEPLOY_SCRIPT} is empty")
    return names


def compare(expected: dict[str, str], target_dir: Path, mode: str) -> list[dict]:
    """Compare a derived copy against the canonical digests it must reproduce.

    Extra files are classified by mode, because the two modes make different
    promises about them:

    ``full``      the copy must be the canonical tree and nothing else, so an
                  extra file is ``unexpected`` — drift. This is not theoretical:
                  the runtime checkout is derived by ``git reset --hard`` with
                  no ``git clean``, so a script deleted from ``main`` lingers on
                  disk and stays importable by the cron entrypoints.
    ``manifest``  ``deploy-runtime.sh`` deliberately preserves files outside the
                  manifest, so an extra file is ``unmanaged`` — informational
                  unless ``--strict``.
    """
    findings: list[dict] = []
    for name, want in sorted(expected.items()):
        dst = target_dir / name
        if not dst.exists():
            findings.append({"file": name, "status": "missing"})
        elif dst.is_symlink() or not dst.is_file():
            findings.append({"file": name, "status": "not-a-regular-file"})
        elif sha256_file(dst) != want:
            findings.append({"file": name, "status": "differs"})
    extra_status = "unexpected" if mode == "full" else "unmanaged"
    for p in sorted(target_dir.iterdir()):
        if p.is_file() and p.name not in expected and p.name not in IGNORED_NAMES:
            findings.append({"file": p.name, "status": extra_status})
    return findings


def parse_copy(value: str) -> tuple[str, str, Path]:
    """Parse a ``LABEL:MODE=PATH`` copy specification."""
    spec, sep, path = value.partition("=")
    label, inner_sep, mode = spec.partition(":")
    if not sep or not path or not inner_sep or not label:
        raise argparse.ArgumentTypeError(f"--copy needs LABEL:MODE=PATH, got: {value}")
    if mode not in COPY_MODES:
        raise argparse.ArgumentTypeError(
            f"unknown copy mode {mode!r} (expected one of {', '.join(COPY_MODES)})"
        )
    return label, mode, Path(path).expanduser()


def build_report(repo_root: Path, ref: str | None, copies: list[tuple[str, str, Path]],
                 strict: bool) -> dict:
    canonical = Canonical(repo_root, ref)
    scripts = canonical.scripts()
    manifest = parse_profile_manifest(
        (canonical.read_bytes(DEPLOY_SCRIPT) or b"").decode("utf-8", errors="replace")
    )
    checks: list[dict] = []

    # --- repo-internal: manifest entries must exist in the canonical tree ---
    missing_manifest = [n for n in manifest if n not in scripts]
    checks.append({
        "check": "profile-manifest-resolves",
        "detail": f"{len(manifest)} manifest entries against {canonical.label()}",
        "ok": not missing_manifest,
        "findings": [{"file": n, "status": "missing"} for n in missing_manifest],
    })

    # --- repo-internal: vendored duplicates must be byte-identical ----------
    vendored_findings = []
    for copy_rel, src_rel in VENDORED:
        src_digest = canonical.digest(src_rel)
        copy_digest = canonical.digest(copy_rel)
        if src_digest is None:
            vendored_findings.append({"file": src_rel, "status": "missing"})
        elif copy_digest is None:
            vendored_findings.append({"file": copy_rel, "status": "missing"})
        elif copy_digest != src_digest:
            vendored_findings.append({"file": copy_rel, "status": "differs"})
    checks.append({
        "check": "vendored-copies-identical",
        "detail": ", ".join(f"{c} == {s}" for c, s in VENDORED),
        "ok": not vendored_findings,
        "findings": vendored_findings,
    })

    # --- derived copies -----------------------------------------------------
    for label, mode, path in copies:
        if not path.is_dir():
            checks.append({
                "check": f"copy:{label}",
                "detail": f"{mode} check of {path}",
                "ok": False,
                "findings": [{"file": str(path), "status": "unreachable"}],
            })
            continue
        expected = scripts if mode == "full" else {n: scripts[n] for n in manifest if n in scripts}
        findings = compare(expected, path, mode)
        # Only 'unmanaged' (manifest mode) is informational; everything else,
        # 'unexpected' included, is drift.
        drift = [f for f in findings if f["status"] != "unmanaged" or strict]
        checks.append({
            "check": f"copy:{label}",
            "detail": f"{mode} check of {path} ({len(expected)} canonical files)",
            "ok": not drift,
            "findings": findings,
        })

    return {
        "canonical": canonical.label(),
        "ok": all(c["ok"] for c in checks),
        "checks": checks,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check derived sports-picks script copies against the canonical scripts/ tree.",
    )
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1],
                        help="canonical repo checkout (default: this script's repo)")
    parser.add_argument("--ref", default=None,
                        help="compare against this git ref instead of the working tree "
                             "(the canonical source is origin/main)")
    parser.add_argument("--copy", action="append", default=[], type=parse_copy,
                        metavar="LABEL:MODE=PATH",
                        help="derived copy to check; MODE is 'full' or 'manifest'. Repeatable.")
    parser.add_argument("--strict", action="store_true",
                        help="treat unmanaged extra files as drift")
    parser.add_argument("--json", action="store_true", help="emit a JSON report")
    args = parser.parse_args(argv)

    try:
        report = build_report(
            args.repo_root.expanduser().resolve(), args.ref, args.copy, args.strict
        )
    except (ProvenanceError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"canonical: {report['canonical']}")
        for c in report["checks"]:
            print(f"[{'ok  ' if c['ok'] else 'DRIFT'}] {c['check']}: {c['detail']}")
            for f in c["findings"]:
                print(f"         {f['status']:<19} {f['file']}")
        print("\nprovenance: clean" if report["ok"]
              else "\nprovenance: DRIFT — see docs/script-provenance.md")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
