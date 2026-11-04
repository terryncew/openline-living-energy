"""Run all tests not requiring OS namespaces on hosts without unshare.

Guard process entry: multiprocessing(spawn) re-imports __main__. Running the
suite at module import would recursively spawn the two-process regression.
"""
from pathlib import Path
import sys
import unittest


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    sys.path[:0] = [str(root), str(root / 'energy/src'), str(root / 'src')]
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for path in sorted((root / 'tests').glob('test_*.py')):
        # test_argon_004 requires user/mount/net namespace isolation.
        if path.name == 'test_argon_004.py':
            continue
        suite.addTests(loader.discover(str(root / 'tests'), pattern=path.name))
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    print('SANDBOX_TESTS_NOT_RUN: host disallows required user namespaces')
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
