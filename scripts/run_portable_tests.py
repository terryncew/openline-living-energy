"""Run full Living Energy test suite, dynamically probing Linux user/mount/net namespaces."""
from pathlib import Path
import os
import subprocess
import sys
import unittest

_NS_PROBE = (
    "import ctypes, os, socket, struct\n"
    "libc = ctypes.CDLL(\"libc.so.6\", use_errno=True)\n"
    "assert libc.mount(b\"tmpfs\", b\"/tmp\", b\"tmpfs\", ctypes.c_ulong(6), b\"size=4096,mode=0700\") == 0\n"
    "s = socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, 0)\n"
    "s.bind((0, 0))\n"
    "ifi = struct.pack(\"BBHiII\", socket.AF_UNSPEC, 0, 0, 1, 1, 1)\n"
    "nlhdr = struct.pack(\"IHHII\", 16 + len(ifi), 16, 0x05, 1, 0)\n"
    "s.send(nlhdr + ifi); s.recv(4096); s.close()\n"
    "print(\"NS_OK\")\n"
)


def _unshare_available() -> bool:
    try:
        real_py = os.path.realpath(sys.executable)
        r = subprocess.run(
            ["unshare", "--user", "--map-root-user", "--mount", "--net", "--pid", "--fork", real_py, "-c", _NS_PROBE],
            capture_output=True, text=True, timeout=5
        )
        return r.returncode == 0 and "NS_OK" in r.stdout
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
        print("SANDBOX_TESTS_NOT_RUN: host disallows required user/mount/net namespaces")
    else:
        print("SANDBOX_TESTS_EXECUTED: Linux user/mount/PID/net namespaces verified")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
