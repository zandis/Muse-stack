#!/usr/bin/env python3
"""Sample this VM's CPU, memory and network once a minute, and run the idle guard.

Standard library only; runs as the unprivileged service user with no network
access (muse-vm-monitor.service). Two jobs:

1. Samples. One JSON line a minute in MUSE_VM_STATE/samples/YYYY-MM.jsonl:
   time, CPU busy %, memory in use %, network bits per second, and how much the
   idle guard holds. `muse-vm-status` turns these into the 7-day figures the
   Devices card shows. Files older than about two months are deleted.

2. Idle guard. Oracle may reclaim an Always Free instance when, over 7 days,
   CPU (95th percentile), network AND memory use are all under 20%. All three
   must be low, so staying above one is enough. Memory is the cheapest: this
   process holds enough otherwise unused memory to keep "memory in use" at
   MUSE_IDLE_GUARD_TARGET_PCT (default 25%), and gives it back as soon as
   anything else needs it. It measures everything else first and only tops up
   the difference: when real work (for example a small embedding model kept
   loaded) already uses that much, it holds nothing.

   Be clear about what this is: a reservation, not useful work. It burns no CPU
   and sends nothing. It is a setting (MUSE_IDLE_GUARD=off turns it off), and
   deploy/oracle-stack/README.md says what Oracle's page does and does not say
   about it.

   The metric it targets is the instance's MemoryUtilization in the
   oci_computeagent metric namespace (OCI Console: the instance, Metrics,
   "Memory Utilization"). This script computes the same idea locally as
   (MemTotal - MemAvailable) / MemTotal from /proc/meminfo; that Oracle's agent
   uses exactly this formula is an assumption to check in the console.

Settings (environment, normally from /etc/muse/vm.conf):
    MUSE_VM_STATE                 where samples go (default /var/lib/muse/vm)
    MUSE_IDLE_GUARD               on | off (default on)
    MUSE_IDLE_GUARD_TARGET_PCT    20.5 to 40 (default 25)
    MUSE_IDLE_GUARD_MAX_MB        the most it may hold (default 3072)

    muse-vm-monitor.py            run for ever
    muse-vm-monitor.py --once     take one sample, adjust once, exit (for tests)
"""

from __future__ import annotations

import json
import mmap
import os
import signal
import sys
import time
from pathlib import Path

INTERVAL_S = 60
CHUNK_MB = 64
HYSTERESIS_MB = 128          # do not chase small changes
KEEP_FREE_PCT = 25.0         # never hold memory when less than this share is available
ORACLE_IDLE_PCT = 20.0       # Oracle's stated threshold for CPU, network and memory
KEEP_MONTHS = 2
PROC = Path(os.environ.get("MUSE_PROC_ROOT", "/proc"))


# ----------------------------------------------------------------- readers

def parse_meminfo(text: str) -> dict[str, int]:
    """/proc/meminfo as bytes per field."""
    out: dict[str, int] = {}
    for line in text.splitlines():
        name, _, rest = line.partition(":")
        parts = rest.split()
        if parts and parts[0].isdigit():
            out[name.strip()] = int(parts[0]) * (1024 if len(parts) > 1 and parts[1] == "kB" else 1)
    return out


def memory_used_pct(meminfo: dict[str, int]) -> float:
    total = meminfo.get("MemTotal", 0)
    if total <= 0:
        return 0.0
    available = meminfo.get("MemAvailable", meminfo.get("MemFree", 0))
    return max(0.0, min(100.0, 100.0 * (total - available) / total))


def parse_cpu(text: str) -> tuple[int, int]:
    """(busy jiffies, total jiffies) from the first line of /proc/stat."""
    for line in text.splitlines():
        if line.startswith("cpu "):
            fields = [int(x) for x in line.split()[1:]]
            idle = fields[3] + (fields[4] if len(fields) > 4 else 0)   # idle + iowait
            total = sum(fields[:8])                                   # guest time is already inside user
            return total - idle, total
    return 0, 0


def parse_net(text: str) -> int:
    """Bytes received plus sent on every interface except loopback (/proc/net/dev)."""
    total = 0
    for line in text.splitlines()[2:]:
        name, _, rest = line.partition(":")
        fields = rest.split()
        if name.strip() == "lo" or len(fields) < 9:
            continue
        total += int(fields[0]) + int(fields[8])
    return total


def _read(name: str) -> str:
    try:
        return (PROC / name).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


# -------------------------------------------------------------- idle guard

def desired_hold_mb(total_bytes: int, available_bytes: int, held_mb: int, target_pct: float, max_mb: int) -> int:
    """How many MB the guard should hold so that memory in use reaches target_pct.

    `available_bytes` is MemAvailable with the current hold in place. What
    everything else uses is measured first; the guard only makes up the
    difference, and holds nothing when that would leave less than
    KEEP_FREE_PCT of memory available.
    """
    if total_bytes <= 0:
        return 0
    mb = 1024 * 1024
    held = held_mb * mb
    others = max(0, total_bytes - available_bytes - held)
    want = int(total_bytes * target_pct / 100.0) - others
    if want <= 0:
        return 0
    # With `want` held, this much would stay available.
    left = total_bytes - others - want
    floor = int(total_bytes * KEEP_FREE_PCT / 100.0)
    if left < floor:
        want = max(0, total_bytes - others - floor)
    want_mb = min(max_mb, want // mb)
    return int(want_mb // CHUNK_MB * CHUNK_MB + (CHUNK_MB if want_mb % CHUNK_MB else 0)) if want_mb else 0


class Reservation:
    """Anonymous memory, touched so it is really resident. Released chunk by chunk."""

    def __init__(self) -> None:
        self.chunks: list[mmap.mmap] = []

    @property
    def held_mb(self) -> int:
        return len(self.chunks) * CHUNK_MB

    def resize(self, target_mb: int) -> None:
        target_chunks = max(0, target_mb // CHUNK_MB)
        block = b"\x01" * (1024 * 1024)
        while len(self.chunks) < target_chunks:
            try:
                chunk = mmap.mmap(-1, CHUNK_MB * 1024 * 1024)
                for offset in range(0, CHUNK_MB * 1024 * 1024, len(block)):
                    chunk[offset:offset + len(block)] = block   # non-zero, so the pages are real
            except (OSError, MemoryError, ValueError):
                break   # the unit's MemoryMax or the machine said no: hold what we have
            self.chunks.append(chunk)
        while len(self.chunks) > target_chunks:
            self.chunks.pop().close()


def guard_settings(environ: dict[str, str] | os._Environ[str]) -> tuple[bool, float, int]:
    enabled = environ.get("MUSE_IDLE_GUARD", "on").strip().lower() not in ("off", "0", "false", "no")
    try:
        target = float(environ.get("MUSE_IDLE_GUARD_TARGET_PCT", "25"))
    except ValueError:
        target = 25.0
    target = max(ORACLE_IDLE_PCT + 0.5, min(40.0, target))
    try:
        max_mb = int(environ.get("MUSE_IDLE_GUARD_MAX_MB", "3072"))
    except ValueError:
        max_mb = 3072
    return enabled, target, max(0, max_mb)


# ----------------------------------------------------------------- samples

def state_dir(environ: dict[str, str] | os._Environ[str]) -> Path:
    return Path(environ.get("MUSE_VM_STATE", "/var/lib/muse/vm"))


def append_sample(directory: Path, sample: dict[str, float]) -> Path:
    samples = directory / "samples"
    samples.mkdir(parents=True, exist_ok=True)
    name = time.strftime("%Y-%m", time.gmtime(sample["t"])) + ".jsonl"
    path = samples / name
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(sample, separators=(",", ":")) + "\n")
    return path


def prune_samples(directory: Path, now: float) -> None:
    samples = directory / "samples"
    year, month = time.gmtime(now)[:2]
    keep = set()
    for _ in range(KEEP_MONTHS + 1):
        keep.add(f"{year:04d}-{month:02d}.jsonl")
        month -= 1
        if month == 0:
            year, month = year - 1, 12
    for path in samples.glob("*.jsonl"):
        if path.name not in keep:
            try:
                path.unlink()
            except OSError:
                pass


def write_guard_state(directory: Path, doc: dict[str, object]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    tmp = directory / "idle-guard.json.tmp"
    tmp.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(directory / "idle-guard.json")


# -------------------------------------------------------------------- loop

def main(argv: list[str]) -> int:
    once = "--once" in argv
    environ = os.environ
    directory = state_dir(environ)
    enabled, target, max_mb = guard_settings(environ)
    reservation = Reservation()
    stop = {"now": False}

    def _stop(_signum: int, _frame: object) -> None:
        stop["now"] = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    busy0, total0 = parse_cpu(_read("stat"))
    net0 = parse_net(_read("net/dev"))
    t0 = time.time()
    if once:
        time.sleep(0.2)
    while not stop["now"]:
        if not once:
            # Sleep in short steps so a stop request is honoured quickly.
            deadline = t0 + INTERVAL_S
            while not stop["now"] and time.time() < deadline:
                time.sleep(min(2.0, max(0.05, deadline - time.time())))
            if stop["now"]:
                break
        now = time.time()
        busy1, total1 = parse_cpu(_read("stat"))
        net1 = parse_net(_read("net/dev"))
        meminfo = parse_meminfo(_read("meminfo"))
        elapsed = max(0.001, now - t0)
        cpu = 100.0 * (busy1 - busy0) / (total1 - total0) if total1 > total0 else 0.0
        bps = max(0.0, (net1 - net0) * 8.0 / elapsed)

        if enabled:
            want = desired_hold_mb(meminfo.get("MemTotal", 0), meminfo.get("MemAvailable", 0),
                                   reservation.held_mb, target, max_mb)
            if want == 0 or abs(want - reservation.held_mb) >= HYSTERESIS_MB:
                reservation.resize(want)
                meminfo = parse_meminfo(_read("meminfo"))
        mem = memory_used_pct(meminfo)
        sample = {"t": round(now), "cpu": round(cpu, 2), "mem": round(mem, 2), "net_bps": round(bps), "guard_mb": reservation.held_mb}
        try:
            append_sample(directory, sample)
            prune_samples(directory, now)
            write_guard_state(directory, {
                "enabled": enabled,
                "target_pct": target,
                "oracle_idle_threshold_pct": ORACLE_IDLE_PCT,
                "held_mb": reservation.held_mb,
                "memory_used_pct": round(mem, 2),
                "metric": "MemoryUtilization (namespace oci_computeagent)",
                "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
            })
        except OSError as exc:
            print(f"muse-vm-monitor: could not write a sample: {exc}", file=sys.stderr)
        busy0, total0, net0, t0 = busy1, total1, net1, now
        if once:
            break
    reservation.resize(0)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
