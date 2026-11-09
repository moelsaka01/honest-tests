import atexit
import importlib.util
import os
import shutil
import sys
import tempfile
from pathlib import Path

# Hermetic temp dir: tests (and the subprocesses they start) must never see or delete another run's
# backup/work folders, e.g. when this suite is the test command of a mutation run on this very repo.
_PRIVATE_TMP = tempfile.mkdtemp(prefix="ht-suite-")
for _k in ("TMPDIR", "TEMP", "TMP"):
    os.environ[_k] = _PRIVATE_TMP
tempfile.tempdir = _PRIVATE_TMP
atexit.register(shutil.rmtree, _PRIVATE_TMP, ignore_errors=True)

ROOT = Path(__file__).resolve().parent.parent
MUTATION_CHECK = ROOT / "skills" / "mutation-check" / "scripts" / "mutation_check.py"
SMELL_SCAN = ROOT / "skills" / "test-roast" / "scripts" / "smell_scan.py"


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod
