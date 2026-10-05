"""GEN-8551: canonical skills enter the existing contract-sync queue."""
import os
import subprocess
from pathlib import Path


def test_skill_source_change_uses_pinned_read_only_clone():
    # intent: a skill-only update must refresh MCP, retain failed work and avoid
    # unrelated consumers; this invokes the real shell source-detection flow.
    root = Path(__file__).resolve().parents[1]
    subprocess.run(
        ['bash', str(root / 'contract-sync/test_skill_source.sh')],
        check=True, timeout=60, env={**os.environ, 'TMPDIR': '/mnt/data/tmp'},
    )
