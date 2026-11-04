"""Run full Living Energy test suite, dynamically probing Linux user namespaces."""
from pathlib import Path
import subprocess
import sys
import unittest


def _unshare_available() -> bool:
    try:
        r = subprocess.run(
            ["unshare", "--user", "--map-root-user", "--mount", "--net", "--pid", "--fork", sys.executable, "-c", "print(1)"],
            capture_output=True, text=True, timeout=5
        )
        return r.returncode == 0
    except Exception:
        return False


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    sys.path[:0] = [str(root), str(root / "energy/src"), str(root / "src")]
    has_ns = _unshare_available()
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for path in sorted((root / "tests").glob("test_*.py")):
        if path.name == "test_argon_004.py" and not has_ns:
            continue
        suite.addTests(loader.discover(str(root / "tests"), pattern=path.name))
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    if not has_ns:
        print("SANDBOX_TESTS_NOT_RUN: host disallows required user namespaces")
    else:
        print("SANDBOX_TESTS_EXECUTED: Linux user/mount/PID/net namespaces verified")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
