#!/usr/bin/env python3
"""T10 external-consumer release gate for the contract-sync retry comparison.

Not a pytest module: it is a standalone CLI a release runs explicitly, in
addition to the nineteen unit/harness cases in
``tests/ops/test_contract_sync_merged_retry.py``. Those cases prove the
orchestration against a fixture catalog module; this gate proves the retry
comparison against the real consumer the retry is for -- the gen-mcp-server
checkout at the reviewed commit -- using that checkout's own
``gen_mcp_server.catalog_sheet`` parse/render and the same image/install flow
``ops/contract-sync/contract-sync.sh`` uses to regenerate it.

    python tests/ops/contract_sync_real_consumer.py \
      --mcp-source /Users/mav/projects/gen-mcp-server \
      --mcp-sha 8868b9d2c9612d073cad1a7bd4c35522a01a67bc

Both arguments are required and a missing one refuses (exit 2) instead of
skipping. The source checkout is only read: history is cloned into a throwaway
directory, HEAD is detached at the requested commit there, and every synthetic
sheet is produced by the canonical parser inside that clone. The gate asserts
the source's HEAD, refs and worktree status are byte-identical afterwards.

Scenarios, all against the real committed ``catalog-sheet.tsv``:

  baseline    committed sheet, unmodified                          -> 0
  stamp-only  every declared served_sha cell rewritten             -> 0
  real cell   one non-stamp cell of the first row rewritten        -> 1
  malformed   one declared column header duplicated                -> 1

Each comparison runs the real helper as a subprocess inside
``python:3.12-bookworm`` after ``pip install -e .``, exactly as
``catalog_sheet_retry_check`` does, and the gate proves the helper's module
resolution: ``gen_mcp_server.catalog_sheet`` must import normally from the
clone's ``src`` (its ``__file__`` inside the clone), not through a fixture
parser.

Sabotage then proves these cases can fail, on copies of the helper inside the
throwaway directory only: a comparison that bypasses the check and a comparison
that blanks real cells both turn the real-cell refusal into 0 (red), and the
pristine helper refuses again after restore.

# intent: the retry's converged decision is only correct if it reads the real
# consumer catalog through the consumer's own parser; a gate that skipped the
# checkout, imported a fixture module, or accepted a real cell difference would
# report a stale contract mirror as synced.
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PINNED_SHA = "8868b9d2c9612d073cad1a7bd4c35522a01a67bc"
CATALOG_PATH = "src/gen_mcp_server/contracts/catalog-sheet.tsv"
MCP_IMAGE = "python:3.12-bookworm"
GIT_TIMEOUT = 300
RUN_TIMEOUT = 900
STAMP_COLUMN = "served_sha"
CONTAINER_MODULE = "/w/src/gen_mcp_server/catalog_sheet.py"

HELPER = Path(__file__).resolve().parents[2] / "ops/contract-sync/catalog_sheet_retry_check.py"

# Sabotage edits applied to copies of the helper in the throwaway directory.
BYPASS_OLD = "def compares(api, committed: str, regenerated: str) -> bool:\n    try:"
BYPASS_NEW = "def compares(api, committed: str, regenerated: str) -> bool:\n    return True\n    try:"
BLANK_OLD = (
    '        for row in rows:\n'
    '            if STAMP_COLUMN in row:\n'
    '                row[STAMP_COLUMN] = ""'
)
BLANK_NEW = "        for row in rows:\n            for name in row:\n                row[name] = \"\""

DRIVER = '''"""Canonical-parser driver for the T10 gate, run inside the MCP image."""
import sys
from pathlib import Path

CATALOG = "src/gen_mcp_server/contracts/catalog-sheet.tsv"
STAMP = "served_sha"
NEW_STAMP = "cafebabecafebabecafebabecafebabecafebabe"


def canonical():
    sys.path.insert(0, "/w/src")
    import gen_mcp_server.catalog_sheet as module
    return module


def main(mode):
    api = canonical()
    if mode == "probe":
        print("MODULE_FILE=%s" % api.__file__)
        print("MODULE_API=%s" % all(hasattr(api, n) for n in ("COLUMNS", "KEYS", "parse", "render")))
        print("PACKAGE_IMPORT=%s" % (api.__name__ == "gen_mcp_server.catalog_sheet"))
        return 0
    path = Path("/w") / CATALOG
    text = path.read_text()
    sections = api.parse(text)
    if api.render(sections) != text:
        print("ROUNDTRIP=0")
        return 1
    print("ROUNDTRIP=1")
    if mode == "stamp":
        for title, rows in sections.items():
            if STAMP not in api.COLUMNS.get(title, ()):
                continue
            for row in rows:
                row[STAMP] = NEW_STAMP
    elif mode == "realcell":
        title, rows = next(iter(sections.items()))
        row = rows[0]
        column = next(c for c in api.COLUMNS[title] if c != STAMP and c in row)
        before = row[column]
        row[column] = "REALCELL" if before != "REALCELL" else "REALCELL2"
    elif mode == "malformed":
        path.write_text(text)  # keep the canonical bytes, then corrupt one header
        blocks = text.strip().split("\\n\\n")
        lines = blocks[0].split("\\n")
        header = lines[1].split("\\t")
        header.insert(header.index(STAMP) + 1, STAMP)
        lines[1] = "\\t".join(header)
        path.write_text("\\n\\n".join(["\\n".join(lines)] + blocks[1:]))
        print("MALFORMED=duplicate_served_sha")
        return 0
    else:
        raise SystemExit("unknown mode " + mode)
    rendered = api.render(sections)
    if rendered == text:
        raise SystemExit("mutation produced no change: " + mode)
    path.write_text(rendered)
    print("MUTATED=%s" % mode)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
'''


def git(repo: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, timeout=GIT_TIMEOUT, check=True,
    )
    return out.stdout


def snapshot(repo: Path) -> dict:
    """Everything the gate promises not to touch on the real checkout."""
    refs = git(repo, "for-each-ref", "--format=%(refname) %(objectname)")
    status = git(repo, "status", "--porcelain=v1", "-uall")
    return {
        "head": git(repo, "rev-parse", "HEAD").strip(),
        "refs": hashlib.sha256(refs.encode()).hexdigest(),
        "status": hashlib.sha256(status.encode()).hexdigest(),
    }


def refuse(message: str, code: int = 1) -> int:
    print("REFUSED: " + message, file=sys.stderr)
    return code


def prepare(source: Path, sha: str, work: Path) -> Path:
    clone = work / "mcp"
    subprocess.run(
        ["git", "clone", "--quiet", "--no-checkout", str(source), str(clone)],
        capture_output=True, text=True, timeout=GIT_TIMEOUT, check=True,
    )
    subprocess.run(
        ["git", "-C", str(clone), "-c", "advice.detachedHead=false", "checkout", "--quiet", "--detach", sha],
        capture_output=True, text=True, timeout=GIT_TIMEOUT, check=True,
    )
    head = git(clone, "rev-parse", "HEAD").strip()
    if head != sha:
        raise SystemExit("clone resolved %s, requested %s" % (head, sha))
    if not (clone / CATALOG_PATH).is_file():
        raise SystemExit("checkout has no " + CATALOG_PATH)
    return clone


def sabotaged(helper_text: str, work: Path, name: str, old: str, new: str) -> Path:
    if old not in helper_text:
        raise SystemExit("sabotage anchor missing from %s: %r" % (name, old.splitlines()[0]))
    path = work / name
    path.write_text(helper_text.replace(old, new, 1))
    return path


def container_script(copies: dict, mode_runs: list) -> str:
    lines = [
        "set -u",
        "git config --global --add safe.directory '*'",
        "pip install -q -e . >/dev/null 2>&1 || { echo 'INSTALL_FAILED=1'; exit 9; }",
        "export PYTHONDONTWRITEBYTECODE=1",
        "python /rc/driver.py probe; echo \"PROBE_RC=$?\"",
    ]
    for label, helper, mutate in mode_runs:
        if mutate:
            lines.append("python /rc/driver.py %s || exit 10" % mutate)
        lines.append('python /rc/%s /w "%s"; echo "RESULT %s rc=$?"' % (helper, CATALOG_PATH, label))
    lines.append('git checkout -- "%s"; git clean -xdfq' % CATALOG_PATH)
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="T10 real-consumer retry comparison gate")
    parser.add_argument("--mcp-source", required=True, help="gen-mcp-server git checkout (read only)")
    parser.add_argument("--mcp-sha", required=True, help="exact reviewed commit, %s" % PINNED_SHA)
    parser.add_argument("--log-dir", default=None, help="durable directory for the raw docker transcript")
    parser.add_argument("--keep", action="store_true", help="keep the throwaway clone for inspection")
    args = parser.parse_args(argv)

    if args.mcp_sha != PINNED_SHA:
        return refuse("--mcp-sha must be the reviewed commit %s, got %s" % (PINNED_SHA, args.mcp_sha))
    source = Path(args.mcp_source).expanduser().resolve()
    if not (source / ".git").exists():
        return refuse("--mcp-source is not a git checkout: %s" % source)
    try:
        git(source, "cat-file", "-e", PINNED_SHA + "^{commit}")
    except subprocess.CalledProcessError:
        return refuse("commit %s is absent from %s (fetch it first)" % (PINNED_SHA, source))
    if not HELPER.is_file():
        return refuse("helper missing: %s" % HELPER)

    before = snapshot(source)
    helper_text = HELPER.read_text()
    helper_digest = hashlib.sha256(helper_text.encode()).hexdigest()

    work = Path(tempfile.mkdtemp(prefix="gen10539-real-consumer-", dir=str(Path.home() / "mnt-data/tmp")))
    log_dir = Path(args.log_dir).expanduser() if args.log_dir else None
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
    transcript = work / "docker.log"

    try:
        clone = prepare(source, PINNED_SHA, work)
        (work / "driver.py").write_text(DRIVER)
        copies = {
            "helper.py": work / "helper.py",
            "helper_bypass.py": sabotaged(helper_text, work, "helper_bypass.py", BYPASS_OLD, BYPASS_NEW),
            "helper_blank_all.py": sabotaged(helper_text, work, "helper_blank_all.py", BLANK_OLD, BLANK_NEW),
        }
        shutil.copyfile(HELPER, copies["helper.py"])
        runs = [
            ("baseline", "helper.py", None),
            ("stamp_only", "helper.py", "stamp"),
            ("real_cell", "helper.py", "realcell"),
            ("sabotage_bypass", "helper_bypass.py", None),
            ("sabotage_blank_real", "helper_blank_all.py", None),
            ("restore_real_cell", "helper.py", None),
            ("malformed", "helper.py", "malformed"),
        ]
        cmd = [
            "docker", "run", "--rm", "--cpus", "2",
            "-v", "%s:/w" % clone,
            "-v", "%s:/rc" % work,
            "-w", "/w", MCP_IMAGE, "sh", "-c", container_script(copies, runs),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=RUN_TIMEOUT)
        transcript.write_text("$ %s\n\n--- stdout ---\n%s\n--- stderr ---\n%s\n--- rc %s ---\n"
                              % (" ".join(cmd), proc.stdout, proc.stderr, proc.returncode))
        if log_dir is not None:
            shutil.copyfile(transcript, log_dir / "real-consumer-docker.log")
        if proc.returncode != 0:
            print(proc.stdout)
            print(proc.stderr, file=sys.stderr)
            return refuse("container flow failed rc=%s (see %s)" % (proc.returncode, transcript))

        results = {}
        for line in proc.stdout.splitlines():
            if line.startswith("MODULE_FILE="):
                results["module_file"] = line.split("=", 1)[1]
            elif line.startswith("MODULE_API="):
                results["module_api"] = line.split("=", 1)[1]
            elif line.startswith("PACKAGE_IMPORT="):
                results["package_import"] = line.split("=", 1)[1]
            elif line.startswith("RESULT "):
                label, rc = line[len("RESULT "):].split(" rc=")
                results[label] = rc
        print(proc.stdout)

        expected = {
            "baseline": "0",
            "stamp_only": "0",
            "real_cell": "1",
            "restore_real_cell": "1",
            "malformed": "1",
            # the sabotage runs must DISAGREE with the real-cell refusal
            "sabotage_bypass": "0",
            "sabotage_blank_real": "0",
        }
        failures = []
        module_file = results.get("module_file", "")
        # inside the container the clone is /w, so the canonical import must land
        # in the clone's own package tree and never on a driver-side fixture file
        if module_file != CONTAINER_MODULE:
            failures.append("module resolved to %r, not %s" % (module_file, CONTAINER_MODULE))
        if results.get("module_api") not in ("1", "True") or results.get("package_import") not in ("1", "True"):
            failures.append("canonical module API/import proof failed: %r/%r"
                            % (results.get("module_api"), results.get("package_import")))
        for label, want in expected.items():
            got = results.get(label)
            if got != want:
                failures.append("%s expected rc=%s got %s" % (label, want, got))
        if hashlib.sha256(HELPER.read_text().encode()).hexdigest() != helper_digest:
            failures.append("the repo helper changed while the gate ran")
        after = snapshot(source)
        for key in ("head", "refs", "status"):
            if before[key] != after[key]:
                failures.append("source %s changed: %s -> %s" % (key, before[key], after[key]))

        if failures:
            for failure in failures:
                print("FAIL " + failure, file=sys.stderr)
            return 1
        print("PASS pinned=%s module=%s baseline=0 stamp_only=0 real_cell=1 malformed=1 "
              "sabotage=detected source=unchanged" % (PINNED_SHA, module_file))
        if log_dir is not None:
            (log_dir / "real-consumer-gate.log").write_text(
                "PASS pinned=%s\nexit=0\n" % PINNED_SHA)
        return 0
    except subprocess.TimeoutExpired:
        return refuse("bounded container run exceeded %ss" % RUN_TIMEOUT)
    finally:
        if not args.keep:
            shutil.rmtree(work, ignore_errors=True)
        else:
            print("kept " + str(work))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
