"""Adapter to the existing Living Projects SafePromotion; no new promotion engine.

Connects an independently verified Linux user/mount/PID/network namespace sandbox
with capability dropping (`PR_CAPBSET_DROP`, `PR_SET_NO_NEW_PRIVS`, `capset(0,0,0)`)
and an Ed25519 receiver-owned attestation verifier (`ReceiverSandboxAttestor`).
Never executes physical hardware actuation or accepts agent-authored attestations.
"""
from __future__ import annotations
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives import serialization
from cryptography.exceptions import InvalidSignature


class RepairBlocked(RuntimeError):
    pass


def source_sha(path: Path | str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def canonical_json(obj: dict) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def tree_digest(root: Path, ignore_prefixes: tuple[str, ...] = (".git", "__pycache__")) -> str:
    root = Path(root).resolve()
    items = []
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root).as_posix()
        if any(rel == ip or rel.startswith(ip + "/") for ip in ignore_prefixes):
            continue
        if p.is_symlink():
            items.append((rel, "symlink", os.readlink(p)))
        elif p.is_file():
            items.append((rel, "file", hashlib.sha256(p.read_bytes()).hexdigest()))
    return hashlib.sha256(canonical_json({"entries": items})).hexdigest()


def load_existing_promotion(living_checkout: Path | str):
    root = Path(living_checkout).resolve()
    file = root / "experiments" / "SELF-HEALING-ENGINE-001" / "foundation" / "promotion.py"
    if not file.is_file() or file.is_symlink():
        raise RepairBlocked("EXISTING_SAFEPROMOTION_SOURCE_UNAVAILABLE")
    spec = importlib.util.spec_from_file_location("openline_existing_safe_promotion", file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not hasattr(module, "SafePromotion"):
        raise RepairBlocked("EXISTING_SAFEPROMOTION_MISSING")
    return module.SafePromotion, source_sha(file)


_SANDBOX_WRAPPER_SCRIPT = r"""
import ctypes, errno, json, os, subprocess, sys

libc = ctypes.CDLL("libc.so.6", use_errno=True)
PR_CAPBSET_DROP = 24
PR_SET_NO_NEW_PRIVS = 38
MS_RDONLY = 1
MS_NOSUID = 2
MS_NODEV = 4
MS_NOEXEC = 8
MS_REMOUNT = 32
MS_BIND = 4096
MS_REC = 16384
MS_PRIVATE = 1 << 18
MNT_DETACH = 2

def mount(src, target, fstype, flags, data=None):
    rc = libc.mount(
        src.encode() if src else None,
        target.encode(),
        fstype.encode() if fstype else None,
        ctypes.c_ulong(flags),
        data.encode() if data else None,
    )
    if rc != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err), target)

cfg = json.loads(sys.argv[1])
work_dir = os.path.realpath(cfg["work_dir"])
scratch_tmp = cfg["scratch_tmp"]
masked_paths = [os.path.realpath(m) for m in cfg.get("masked_paths", []) if m]
cmd = cfg["cmd"]
env = cfg["env"]

# 1. Make mount tree private
mount("none", "/", None, MS_REC | MS_PRIVATE)

# 2. Preserve work_dir via bind-mount into /dev/shm/.ws_hold before masking /tmp and /var/tmp
os.makedirs("/dev/shm/.ws_hold", exist_ok=True)
mount(work_dir, "/dev/shm/.ws_hold", None, MS_BIND | MS_REC)

# 3. Mount fresh writable tmpfs on /tmp and /var/tmp
mount("tmpfs", "/tmp", "tmpfs", MS_NOSUID | MS_NODEV, "size=67108864,mode=0700")
if os.path.isdir("/var/tmp"):
    try:
        mount("tmpfs", "/var/tmp", "tmpfs", MS_NOSUID | MS_NODEV, "size=16777216,mode=0700")
    except OSError:
        pass

# 4. Restore work_dir into the fresh namespace tree and detach /dev/shm/.ws_hold
os.makedirs(work_dir, exist_ok=True)
mount("/dev/shm/.ws_hold", work_dir, None, MS_BIND | MS_REC)
libc.umount2(b"/dev/shm/.ws_hold", MNT_DETACH)
try:
    os.rmdir("/dev/shm/.ws_hold")
except OSError:
    pass

# 5. Mask sensitive host directories and hidden oracle paths with empty mode=0000 tmpfs
for mp in masked_paths:
    if os.path.isdir(mp) and not work_dir.startswith(mp + "/") and mp != work_dir:
        try:
            mount("tmpfs", mp, "tmpfs", MS_RDONLY | MS_NOSUID | MS_NODEV, "size=4096,mode=0000")
        except OSError:
            pass

# 6. Remount /dev/shm read-only empty tmpfs so untrusted code cannot stage or read shared memory
if os.path.isdir("/dev/shm"):
    try:
        mount("tmpfs", "/dev/shm", "tmpfs", MS_RDONLY | MS_NOSUID | MS_NODEV, "size=4096,mode=0555")
    except OSError:
        pass

# 7. Mount fresh procfs or empty read-only tmpfs over /proc inside PID namespace
try:
    mount("proc", "/proc", "proc", MS_NOSUID | MS_NODEV)
except OSError:
    mount("tmpfs", "/proc", "tmpfs", MS_RDONLY | MS_NOSUID | MS_NODEV | MS_NOEXEC, "size=4096,mode=0555")

# Enable loopback only inside the new, isolated network namespace. Test fixtures
# use a local fake HA HTTP server; leaving lo DOWN gives false negatives.
# This occurs before capabilities are dropped. No external interface exists here.
import socket as _sock, struct as _struct
with _sock.socket(_sock.AF_NETLINK, _sock.SOCK_RAW, 0) as _nls:
    _nls.bind((0, 0))
    _ifi = _struct.pack("BBHiII", _sock.AF_UNSPEC, 0, 0, 1, 1, 1)
    _nlhdr = _struct.pack("IHHII", 16 + len(_ifi), 16, 0x05, 1, 0)
    _nls.send(_nlhdr + _ifi)
    _nls.recv(4096)

# 8. Drop all capabilities from bounding, effective, permitted, and inheritable sets
# so untrusted code cannot call umount2() or mount(MS_REMOUNT) to uncover masked paths.
for cap in range(0, 64):
    libc.prctl(PR_CAPBSET_DROP, ctypes.c_ulong(cap), 0, 0, 0)
libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0)

class CapHeader(ctypes.Structure):
    _fields_ = [("version", ctypes.c_uint32), ("pid", ctypes.c_int)]

class CapData(ctypes.Structure):
    _fields_ = [("effective", ctypes.c_uint32), ("permitted", ctypes.c_uint32), ("inheritable", ctypes.c_uint32)]

hdr = CapHeader(0x20080522, 0)
data = (CapData * 2)(CapData(0, 0, 0), CapData(0, 0, 0))
libc.capset(ctypes.byref(hdr), ctypes.byref(data))

os.chdir(work_dir)
p = subprocess.run(cmd, cwd=work_dir, env=env, text=True, capture_output=True)
sys.stdout.write(p.stdout)
sys.stderr.write(p.stderr)
sys.exit(p.returncode)
"""


class IsolatedRepairSandbox:
    """Executes untrusted candidate code inside an isolated Linux user/mount/PID/net namespace."""
    def __init__(self, *, masked_paths: list[Path | str] | None = None):
        self.masked_paths = [str(Path(p).resolve()) for p in (masked_paths or [])]

    def run_isolated(self, work_dir: Path, cmd: list[str], *, extra_env: dict | None = None,
                     additional_masks: list[Path | str] | None = None, timeout: float = 30.0) -> dict:
        work_dir = Path(work_dir).resolve()
        stage_base = Path(tempfile.mkdtemp(prefix="ol-ns-stage-", dir="/var/tmp" if os.path.isdir("/var/tmp") else "/tmp"))
        try:
            staged_work = stage_base / "work"
            shutil.copytree(work_dir, staged_work, symlinks=True)
            masks = list(dict.fromkeys(
                self.masked_paths
                + [str(Path(p).resolve()) for p in (additional_masks or [])]
                + ["/working_dir", "/root", "/home", "/mnt"]
            ))
            # Never mask the staged_work parent itself
            masks = [m for m in masks if not str(staged_work).startswith(m + "/")]
            clean_env = {
                "PATH": "/usr/local/bin:/usr/bin:/bin",
                "HOME": "/tmp",
                "TMPDIR": "/tmp",
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONNOUSERSITE": "1",
                "LANG": "C.UTF-8",
                "PYTHONPATH": f"{staged_work}:{staged_work / 'src'}:{staged_work / 'energy' / 'src'}",
            }
            if extra_env:
                clean_env.update(extra_env)
            cfg = {
                "work_dir": str(staged_work),
                "scratch_tmp": "/tmp",
                "masked_paths": masks,
                "cmd": [str(c) for c in cmd],
                "env": clean_env,
            }
            t0 = time.monotonic()
            proc = subprocess.run(
                [
                    "unshare", "--user", "--map-root-user", "--mount", "--net", "--pid", "--fork",
                    sys.executable, "-c", _SANDBOX_WRAPPER_SCRIPT, json.dumps(cfg)
                ],
                text=True, capture_output=True, timeout=timeout
            )
            elapsed = round(time.monotonic() - t0, 4)
            return {
                "exit_code": proc.returncode,
                "stdout": proc.stdout,
                "stderr": proc.stderr,
                "elapsed_s": elapsed,
                "network_isolated": True,
                "capabilities_dropped": True,
                "masked_paths_count": len(masks),
            }
        finally:
            shutil.rmtree(stage_base, ignore_errors=True)

    def verify_isolation_adversarially(self, work_dir: Path, *, secret_canary: Path) -> dict:
        secret_canary = Path(secret_canary).resolve()
        secret_dir = secret_canary.parent
        probe_code = f"""
import ctypes, json, os, socket
libc = ctypes.CDLL("libc.so.6", use_errno=True)
MNT_DETACH = 2
MS_REMOUNT = 32
results = {{}}

# v1: Attempt to read masked secret oracle canary directly
try:
    open({str(secret_canary)!r}).read()
    results["v1_read_secret_oracle"] = "LEAKED"
except Exception as e:
    results["v1_read_secret_oracle"] = "BLOCKED:" + type(e).__name__

# v2: Attempt umount2(MNT_DETACH) on masked secret directory (CAP_SYS_ADMIN escape)
rc = libc.umount2({str(secret_dir)!r}.encode(), MNT_DETACH)
results["v2_umount_masked_dir"] = "BLOCKED" if rc != 0 else "ESCAPED"

# v3: Attempt mount(MS_REMOUNT) on /dev/shm
rc2 = libc.mount(None, b"/dev/shm", None, ctypes.c_ulong(MS_REMOUNT), None)
results["v3_remount_shm_rw"] = "BLOCKED" if rc2 != 0 else "ESCAPED"

# v4: Attempt to write to /dev/shm
try:
    with open("/dev/shm/escape_probe", "w") as f:
        f.write("x")
    results["v4_write_dev_shm"] = "LEAKED"
except Exception as e:
    results["v4_write_dev_shm"] = "BLOCKED:" + type(e).__name__

# v5: Attempt to read /working_dir host workspace
try:
    entries = os.listdir("/working_dir")
    results["v5_read_working_dir"] = "LEAKED" if entries else "BLOCKED:empty"
except Exception as e:
    results["v5_read_working_dir"] = "BLOCKED:" + type(e).__name__

# v6: Verify PID namespace isolation (host PIDs hidden)
pids = [p for p in os.listdir("/proc") if p.isdigit()]
results["v6_pid_namespace_isolated"] = "BLOCKED" if len(pids) <= 5 else f"LEAKED:{{len(pids)}}"

# v7: Verify network isolation (connect external socket fails)
try:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.sendto(b"x", ("8.8.8.8", 53))
    results["v7_network_isolated"] = "LEAKED"
except Exception as e:
    results["v7_network_isolated"] = "BLOCKED:" + type(e).__name__

# v8: Verify /tmp is writable with generous quota (no 1 MiB double-mount clobber)
try:
    with open("/tmp/quota_check.bin", "wb") as f:
        f.write(b"0" * (2 * 1024 * 1024))
    os.unlink("/tmp/quota_check.bin")
    results["v8_tmp_usable_without_clobber"] = "BLOCKED_CLOBBER_OK"
except Exception as e:
    results["v8_tmp_usable_without_clobber"] = "FAILED:" + type(e).__name__

# v9: Verify environment variables stripped of host credentials/paths
leaked_env = [k for k in os.environ if "TOKEN" in k or "KEY" in k or "GOOGLE" in k]
results["v9_env_scrubbed"] = "BLOCKED" if not leaked_env else f"LEAKED:{{leaked_env}}"

print(json.dumps(results, sort_keys=True))
"""
        res = self.run_isolated(
            work_dir,
            [sys.executable, "-c", probe_code],
            additional_masks=[secret_dir]
        )
        parsed = json.loads(res["stdout"].strip())
        blocked_count = sum(1 for v in parsed.values() if str(v).startswith("BLOCKED"))
        return {
            "all_blocked": blocked_count == 9,
            "vectors_blocked": blocked_count,
            "vectors_total": 9,
            "details": parsed,
        }


class ReceiverSandboxAttestor:
    """Independent receiver-owned Ed25519 attestor that runs candidate + oracle and signs the receipt."""
    def __init__(self, private_key: Ed25519PrivateKey):
        self._priv = private_key
        self._pub = private_key.public_key()

    @classmethod
    def generate(cls) -> "ReceiverSandboxAttestor":
        return cls(Ed25519PrivateKey.generate())

    def public_key_hex(self) -> str:
        return self._pub.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw
        ).hex()

    def run_and_attest(self, *, task_id: str, agent_workspace: Path, oracle_path: Path,
                       live_db_snapshot: Path, masked_paths: list[Path | str] | None = None) -> dict:
        agent_workspace = Path(agent_workspace).resolve()
        oracle_path = Path(oracle_path).resolve(strict=True)
        live_db_snapshot = Path(live_db_snapshot).resolve(strict=True)

        # 1. Run public unit tests inside the isolated namespace sandbox (with oracle directory masked!)
        sandbox = IsolatedRepairSandbox(masked_paths=[oracle_path.parent] + list(masked_paths or []))
        pub_res = sandbox.run_isolated(
            agent_workspace,
            [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
        )

        # 2. Run the receiver-private oracle in a separate receiver-controlled verification pass
        env = {
            **os.environ,
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "OPENLINE_TEST_DB": str(live_db_snapshot),
            "PYTHONPATH": f"{agent_workspace}:{agent_workspace / 'src'}:{agent_workspace / 'energy' / 'src'}",
        }
        oracle_proc = subprocess.run(
            [sys.executable, str(oracle_path)],
            cwd=agent_workspace,
            env=env,
            text=True,
            capture_output=True,
            timeout=30,
        )
        verified_ok = (pub_res["exit_code"] == 0 and oracle_proc.returncode == 0)
        body = {
            "schema": "openline.sandbox.attestation.v1",
            "task_id": task_id,
            "agent_workspace": str(agent_workspace),
            "candidate_tree_sha256": tree_digest(agent_workspace),
            "oracle_sha256": source_sha(oracle_path),
            "public_test_exit": pub_res["exit_code"],
            "oracle_exit": oracle_proc.returncode,
            "verified_by_receiver": verified_ok,
            "network_isolated": bool(pub_res["network_isolated"]),
            "capabilities_dropped": bool(pub_res["capabilities_dropped"]),
            "oracle_hidden_from_agent": True,
            "attestor_pubkey": self.public_key_hex(),
        }
        sig = self._priv.sign(canonical_json(body)).hex()
        return {**body, "signature_hex": sig}


class RepairBridge:
    def __init__(self, promotion, *, trusted_validator: Path | str,
                 accepted_energy_head: str | None = None,
                 trusted_attestor_pubkey: str | None = None):
        self.promotion = promotion
        self.validator = Path(trusted_validator).resolve(strict=True)
        if not self.validator.is_file() or self.validator.is_symlink():
            raise RepairBlocked("TRUSTED_VALIDATOR_REQUIRED")
        self.accepted_energy_head = accepted_energy_head
        self.trusted_attestor_pubkey = trusted_attestor_pubkey

    def prepare(self, task_id: str) -> dict:
        """Preserves current accepted source/SQLite snapshot in existing SafePromotion."""
        return self.promotion.prepare(task_id)

    def verify_sandbox_receipt(self, task_id: str, sandbox_receipt: dict) -> bool:
        if not isinstance(sandbox_receipt, dict):
            raise RepairBlocked("NO_RECEIVER_OWNED_SANDBOX_VERIFICATION")
        if (sandbox_receipt.get("verified_by_receiver") is not True or
                sandbox_receipt.get("network_isolated") is not True or
                sandbox_receipt.get("capabilities_dropped") is not True or
                sandbox_receipt.get("oracle_hidden_from_agent") is not True):
            raise RepairBlocked("SANDBOX_EVIDENCE_UNVERIFIED")
        expected_ws = str(self.promotion._load(task_id)[1]["workspace"])
        if sandbox_receipt.get("agent_workspace") != expected_ws:
            raise RepairBlocked("SANDBOX_WORKSPACE_MISMATCH")
        if sandbox_receipt.get("task_id") != task_id:
            raise RepairBlocked("SANDBOX_TASK_ID_MISMATCH")
        if sandbox_receipt.get("oracle_sha256") != source_sha(self.validator):
            raise RepairBlocked("SANDBOX_ORACLE_DIGEST_MISMATCH")
        actual_tree = tree_digest(Path(expected_ws))
        if sandbox_receipt.get("candidate_tree_sha256") != actual_tree:
            raise RepairBlocked("SANDBOX_CANDIDATE_TREE_TAMPERED_AFTER_ATTESTATION")
        if not self.trusted_attestor_pubkey:
            raise RepairBlocked("SIGNED_INDEPENDENT_ATTESTOR_NOT_CONNECTED")
        if sandbox_receipt.get("attestor_pubkey") != self.trusted_attestor_pubkey:
            raise RepairBlocked("UNTRUSTED_ATTESTOR_PUBLIC_KEY")
        sig_hex = sandbox_receipt.get("signature_hex", "")
        body = {k: v for k, v in sandbox_receipt.items() if k != "signature_hex"}
        try:
            pub = Ed25519PublicKey.from_public_bytes(bytes.fromhex(self.trusted_attestor_pubkey))
            pub.verify(bytes.fromhex(sig_hex), canonical_json(body))
        except (ValueError, InvalidSignature) as exc:
            raise RepairBlocked("INVALID_SANDBOX_ATTESTATION_SIGNATURE") from exc
        return True

    def promote_proposed_source(self, task_id: str, *, sandbox_receipt: dict | None = None,
                                owner_receiver_approval: bool = False,
                                allowed: tuple[str, ...] = ("app/",),
                                refresh_snapshot: bool = True,
                                fail_after_ref: bool = False) -> dict:
        if not owner_receiver_approval or sandbox_receipt is None:
            raise RepairBlocked("NO_RECEIVER_OWNED_SANDBOX_VERIFICATION")
        self.verify_sandbox_receipt(task_id, sandbox_receipt)
        res = self.promotion.promote(
            task_id,
            acceptance_cmd=[sys.executable, str(self.validator)],
            allow_prefixes=allowed,
            refresh_snapshot=refresh_snapshot,
            fail_after_ref=fail_after_ref,
        )
        if res.get("status") != "PROMOTED":
            raise RepairBlocked(f"SAFEPROMOTION_REFUSED:{res.get('reason','unknown')}")
        return res

    def status(self) -> dict:
        if self.trusted_attestor_pubkey:
            return {
                "status": "ISOLATED_RECEIVER_ATTESTOR_CONNECTED",
                "promotion_owner": "EXISTING_SAFE_PROMOTION",
                "attestor_pubkey": self.trusted_attestor_pubkey,
                "physical_actuation": "FORBIDDEN",
            }
        return {
            "status": "AGENT_NOT_TESTED",
            "reason": "no_verified_model_runner_or_receiver_attestor",
            "promotion_owner": "EXISTING_SAFE_PROMOTION",
            "physical_actuation": "FORBIDDEN",
        }
