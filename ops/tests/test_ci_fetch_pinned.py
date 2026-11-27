"""Run real Git transport cases through the existing CI and release pytest gate."""

import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize("case", ["T1", "T2", "T3", "T4", "T5", "T6", "T8", "T9"])
def test_ci_fetch_pinned(case):
    # intent: a broken transport contract must fail the same pytest command used by both gates.
    subprocess.run(
        ["bash", str(Path(__file__).with_suffix(".sh")), case],
        check=True,
        timeout=185,
    )
