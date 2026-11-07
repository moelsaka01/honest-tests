import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MUTATION_CHECK = ROOT / "skills" / "mutation-check" / "scripts" / "mutation_check.py"
SMELL_SCAN = ROOT / "skills" / "test-roast" / "scripts" / "smell_scan.py"


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod
