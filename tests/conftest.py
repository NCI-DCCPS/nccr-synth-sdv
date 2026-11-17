"""Keep pytest-created files inside this experiment."""
from datetime import datetime, timezone
from pathlib import Path
import os
import sys
import tempfile

EXPERIMENT = Path(__file__).resolve().parents[1]
WORK = EXPERIMENT / "work"
sys.dont_write_bytecode = True
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
os.environ["TMPDIR"] = str(WORK / "tmp")
(WORK / "tmp").mkdir(parents=True, exist_ok=True)
tempfile.tempdir = str(WORK / "tmp")


def pytest_configure(config):
    if config.option.basetemp is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        config.option.basetemp = str(WORK / "tmp" / ("pytest-" + stamp))
    path = Path(config.option.basetemp).resolve()
    if not path.is_relative_to(WORK.resolve()) or path == WORK.resolve():
        raise ValueError("pytest basetemp must be below gen-sdv/work")

