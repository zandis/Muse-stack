"""The existing catalog validator must keep passing on this branch."""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_validate_config_script():
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "validate_config.py")],
        check=False,
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "config ok" in proc.stdout
