"""Contract tests for GEN-9831: the QA docker prune must retain locally built CI images.

The hourly ``/usr/local/bin/qa-docker-prune.sh`` removes unused tagged images older
than the keep window. Locally built CI images (``/opt/ci-images``) have no registry and
Woodpecker runs them with ``pull: false``, so an age-based removal breaks every pipeline
of that repo until a person rebuilds by hand (2026-10-12: pipelines 747 and 749 failed
with "pull access denied for limitless-data-ci" after the prune removed it).

These tests execute the committed script's prune decision loop with a fake command
boundary: ``docker``, ``df``, ``journalctl`` and ``logger`` are faked on a prepended
``PATH``, and only the two environment/command-boundary lines of the script are rewritten
(``export PATH=`` to place the fakes, ``LOG=`` to redirect the log to a temp file). The
keep-list ``case`` pattern, the in-use check, the age check and the ``docker rmi``
invocation all execute the committed bytes unchanged. No real docker daemon, prune or
``rmi`` is ever invoked.

The committed script lives at ``ops/qa-docker-prune/qa-docker-prune.sh`` (captured by the
builder). Until then every case is a strict xfail that raises ``AssertionError`` (never an
import/setup error); the builder removes each marker as its case passes, unedited.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
COMMITTED_SCRIPT = REPO_ROOT / "ops" / "qa-docker-prune" / "qa-docker-prune.sh"

# The 13 image refs from the GEN-9831 contract (9831-contract-plan.md), 100h old and
# unused by any container in every case below.
PROTECTED_CI_REFS = (
    "limitless-data-ci:py310",
    "gbv2-ci:latest",
    "day-ci:node22",
    "gen-agentic-ci:py311",
    "limitless-fe-ci:node24",
    "xel-ci-node:node22",
    "xel-ci-py:py312",
)
PROTECTED_QA_CIFIX_REFS = (
    "ag-cifix-qa:01a11ebb",
    "mcp-cifix-qa:b4abc0c5",
)
UNPROTECTED_REFS = (
    "foo:latest",
    "postgres:16",
    "woodpeckerci/plugin-git:2",
    "python:3.11",
)

_contract_xfail = pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "GEN-9831: the committed prune script is not yet captured at "
        "ops/qa-docker-prune/qa-docker-prune.sh; the builder removes this marker "
        "once the case passes"
    ),
)


def _script_path() -> Path:
    override = os.environ.get("QA_DOCKER_PRUNE_SCRIPT")
    path = Path(override).expanduser() if override else COMMITTED_SCRIPT
    if not path.is_file():
        raise AssertionError(
            "committed prune script missing at "
            f"{path}; GEN-9831 builder must capture the live script at "
            "ops/qa-docker-prune/qa-docker-prune.sh"
        )
    return path


def _run_prune(refs: tuple[str, ...]) -> Counter:
    """Run the committed prune script with a fake command boundary and count rmi calls.

    Returns a Counter of ``ref -> number of docker rmi calls`` recorded by the fake
    ``docker``. Every ref in ``refs`` is presented 100h old and unused, so the only
    thing that decides keep-vs-remove is the script's keep-list ``case`` pattern plus
    the (empty) in-use check.
    """
    script = _script_path()
    text = script.read_text()

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        bindir = tmp / "bin"
        bindir.mkdir()
        rmi_log = tmp / "rmi.log"
        images_file = tmp / "images.txt"
        in_use_file = tmp / "in_use.txt"
        rmi_log.write_text(
            ""
        )  # created up front so a fully-protected set yields an empty Counter

        created = (datetime.now(timezone.utc) - timedelta(hours=100)).strftime(
            "%Y-%m-%d"
        )
        images_file.write_text(
            "".join(f"{ref} sha256:fake {created}\n" for ref in refs)
        )
        in_use_file.write_text(
            ""
        )  # every ref is unused by any container per the contract

        # Fake docker: record `docker rmi <ref>`, stub the read-only inventory calls so
        # no real docker daemon is contacted. `volume|image|builder` prune subcommands
        # report a no-op reclaim; the loop removes tagged images only via `rmi`.
        (bindir / "docker").write_text(
            "#!/bin/bash\n"
            'case "$1" in\n'
            '  images) cat "$IMAGES_FILE" ;;\n'
            '  ps) cat "$IN_USE_FILE" ;;\n'
            '  rmi) printf \'%s\\n\' "$2" >> "$RMI_LOG" ;;\n'
            "  volume|image|builder) echo 'Total reclaimed space: 0B' ;;\n"
            '  *) echo "fake docker: unexpected argv: $*" >&2; exit 1 ;;\n'
            "esac\n"
        )
        # `df --output=pcent /` feeds the keep-window selection: 60% keeps the 72h window.
        (bindir / "df").write_text(
            "#!/bin/bash\n"
            'if [ "$1" = --output=pcent ]; then\n'
            "  echo '60%'\n"
            "else\n"
            "  echo 'Filesystem      Size  Used Avail Use% Mounted on'\n"
            "  echo '/dev/root       100G   40G   60G  60% /'\n"
            "fi\n"
        )
        # journalctl and logger are side effects, never decision inputs; stubbed so the
        # test never vacuums the host journal or writes to syslog.
        (bindir / "journalctl").write_text("#!/bin/bash\nexit 0\n")
        (bindir / "logger").write_text("#!/bin/bash\nexit 0\n")
        for name in ("docker", "df", "journalctl", "logger"):
            (bindir / name).chmod(0o755)

        # Rewrite only the two command-boundary/environment lines. The keep-list case
        # pattern, the in-use check, the age check and the rmi call stay byte-identical.
        lines = text.splitlines()
        rewrote_path = False
        rewrote_log = False
        for i, line in enumerate(lines):
            if line.startswith("export PATH="):
                lines[i] = f'export PATH="{bindir}:{line[len("export PATH=") :]}"'
                rewrote_path = True
            elif line.startswith("LOG="):
                lines[i] = f"LOG={tmp / 'qa-docker-prune.log'}"
                rewrote_log = True
        if not rewrote_path:
            raise AssertionError(
                "committed script has no `export PATH=` line to place the fake command boundary"
            )
        if not rewrote_log:
            raise AssertionError(
                "committed script has no `LOG=` line to redirect the prune log to a temp path"
            )

        run_script = tmp / "qa-docker-prune.sh"
        run_script.write_text("\n".join(lines) + "\n")
        run_script.chmod(0o755)

        env = os.environ.copy()
        env["IMAGES_FILE"] = str(images_file)
        env["IN_USE_FILE"] = str(in_use_file)
        env["RMI_LOG"] = str(rmi_log)

        proc = subprocess.run(
            ["bash", str(run_script)],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert proc.returncode == 0, (
            f"prune script exited {proc.returncode}: {proc.stderr}"
        )

        return Counter(rmi_log.read_text().splitlines())


def test_t1_ci_image_tags_are_kept_with_zero_rmi():
    # intent: locally built CI images (pull: false, no registry) must survive the age
    # prune; without the *-ci:* / *-ci-*:* keep patterns the loop issues `docker rmi`
    # for every one of them after 72h, which broke limitless-data pipelines (GEN-9831).
    rmi = _run_prune(PROTECTED_CI_REFS)
    for ref in PROTECTED_CI_REFS:
        assert rmi[ref] == 0, (
            f"protected CI image {ref} was queued for docker rmi {rmi[ref]}x"
        )
    assert sum(rmi.values()) == 0, f"expected zero rmi, got {dict(rmi)}"


def test_t2_qa_cifix_kept_and_unprotected_removed_exactly_once():
    # intent: the pre-existing *-qa* / *cifix* protections stay, and exactly the four
    # stale unmatched tags are removed once each; protecting every image or dropping
    # the qa/cifix patterns both fail this.
    rmi = _run_prune(PROTECTED_QA_CIFIX_REFS + UNPROTECTED_REFS)
    for ref in PROTECTED_QA_CIFIX_REFS:
        assert rmi[ref] == 0, (
            f"protected qa/cifix image {ref} was queued for docker rmi {rmi[ref]}x"
        )
    for ref in UNPROTECTED_REFS:
        assert rmi[ref] == 1, (
            f"stale unmatched image {ref} should be removed exactly once, got {rmi[ref]}"
        )
    assert sorted(rmi) == sorted(UNPROTECTED_REFS), f"unexpected rmi set: {dict(rmi)}"


def test_t3_script_passes_bash_syntax_check():
    # intent: the committed script must be valid bash; a broken case statement (missing
    # esac) or any other syntax error must fail the syntax check before installation.
    script = _script_path()
    proc = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
    assert proc.returncode == 0, f"bash -n failed: {proc.stderr}"


def test_t3_missing_esac_fails_syntax_check():
    # intent: sabotage — deleting the closing esac must make bash -n fail, proving the
    # syntax gate is a real check and not a no-op.
    script = _script_path()
    text = script.read_text()
    assert "esac" in text, (
        "committed script must contain an esac for this sabotage to be meaningful"
    )
    with tempfile.TemporaryDirectory() as td:
        sabotaged = Path(td) / "qa-docker-prune.sh"
        sabotaged.write_text(text.replace("esac", ""))
        proc = subprocess.run(
            ["bash", "-n", str(sabotaged)], capture_output=True, text=True
        )
        assert proc.returncode != 0, "bash -n should fail after removing esac"
