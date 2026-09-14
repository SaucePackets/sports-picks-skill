"""Season warnings must work when date returns zero-prefixed months."""
import os
from pathlib import Path
import subprocess
import pytest

ROOT = Path(__file__).resolve().parents[1]

@pytest.mark.parametrize("sport", ["mlb", "nba", "nfl", "nhl"])
@pytest.mark.parametrize("month", ["08", "09"])
def test_zero_prefixed_month(tmp_path, sport, month):
    date = tmp_path / "date"
    date.write_text(f"#!/bin/sh\necho {month}\n")
    date.chmod(0o755)
    env = dict(os.environ, PATH=str(tmp_path) + os.pathsep + os.environ["PATH"])
    result = subprocess.run(["bash", str(ROOT / f"skills/{sport}-data/scripts/validate_params.sh")],
                            env=env, capture_output=True, text=True)
    assert result.returncode == 0
    assert result.stderr == ""
    expected = sport in {"nba", "nhl"} or (sport == "nfl" and month == "08")
    assert ("WARNING" in result.stdout) == expected
