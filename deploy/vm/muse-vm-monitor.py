#!/usr/bin/env python3
"""Sample this VM's CPU, memory and network once a minute, and run the idle guard.

Standard library only; runs as the unprivileged service user with no network
access (muse-vm-monitor.service). Two jobs:

1. Samples. One JSON line a minute in MUSE_VM_STATE/samples/YYYY-MM.jsonl:
   time, CPU busy %, memory in use %, network bits per second, and how many
   seconds of that minute the idle guard spent working. `muse-vm-status` turns
   these into the 7-day figures the Devices card shows. Files older than about
   two months are deleted.

2. Idle guard. Oracle's Always Free page says an Always Free instance may be
   reclaimed when, over 7 days, CPU utilization for the 95th percentile,
   network utilization and (A1 shapes only) memory utilization are all under
   20%. All three must be low, so keeping one above 20% is enough. The guard
   uses the processor only, because the processor rule applies to every shape
   and a 95th percentile needs just over 5% of the minutes to be busy:

   - When no minute in the last GUARD_EVERY_MIN (10) reached the target
     (MUSE_IDLE_GUARD_TARGET_PCT, default 25%) by itself, it runs the processor
     at about the target for one wall-clock minute (and a few seconds past it,
     in case Oracle's minute and ours are not exactly aligned). That is
     about one minute in ten, twice what the 95th percentile needs, and adds
     roughly 2.5% to the week's average processor use.
   - Real work comes first. Any minute that reaches the target by itself
     restarts the count, so a busy helper never triggers the guard. During a
     guard minute it measures everything else once a second and stops at once
     when other work uses more than BACK_OFF_PCT of the processor; the unit
     also runs it at the lowest priority (Nice=19, CPUWeight=1).
   - It never holds memory and never sends traffic (the unit has no network
     at all).
   - Once a day it writes one plain line to the journal saying what it did
     and why.

   Be clear about what this is: deliberate processor use to stay above a
   threshold, not useful work. It is a setting: MUSE_IDLE_GUARD=on|off|auto.
   `auto` (the default) means on for an Always Free account and off for a paid
   one (MUSE_ACCOUNT_TYPE=always_free|paid, default always_free).
   deploy/oracle-stack/README.md says what Oracle's page does and does not say
   about it.

   The metric it targets is the instance's CpuUtilization in the
   oci_computeagent metric namespace (OCI Console: the instance, Metrics,
   "CPU Utilization"). Locally it is computed from /proc/stat over the whole
   machine; that Oracle's agent uses one-minute buckets on the wall clock is an
   assumption to check in the console.

Settings (environment, normally from /etc/muse/vm.conf):
    MUSE_VM_STATE                 where samples go (default /var/lib/muse/vm)
    MUSE_ACCOUNT_TYPE             always_free | paid (default always_free)
    MUSE_IDLE_GUARD               auto | on | off (default auto)
    MUSE_IDLE_GUARD_TARGET_PCT    20.5 to 40 (default 25)

    muse-vm-monitor.py            run for ever
    muse-vm-monitor.py --once     take one sample, exit (for tests)
"""

from __future__ import annotations

import json
import os
import signal
import sys
import threading
import time
from pathlib import Path

INTERVAL_S = 60
ORACLE_IDLE_PCT = 20.0       # Oracle's stated threshold for CPU, network and memory
GUARD_EVERY_MIN = 10         # at least one minute in this many must reach the target
GUARD_MARGIN_S = 5           # a guard minute runs this much past the wall-clock minute
BACK_OFF_PCT = 10.0          # other work above this share of the processor stops a guard minute
MAX_DUTY = 0.9               # one thread never runs more than this share of one core
SLICE_S = 0.1                # busy/sleep period of the guard's duty cycle
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


def cpu_count() -> int:
    try:
        return max(1, len(os.sched_getaffinity(0)))
    except (AttributeError, OSError):
        return max(1, os.cpu_count() or 1)


# -------------------------------------------------------------- idle guard

def guard_settings(environ: dict[str, str] | os._Environ[str]) -> tuple[bool, float, str]:
    """(on, target %, why) from MUSE_IDLE_GUARD, MUSE_ACCOUNT_TYPE and MUSE_IDLE_GUARD_TARGET_PCT."""
    account = environ.get("MUSE_ACCOUNT_TYPE", "always_free").strip().lower() or "always_free"
    if account not in ("always_free", "paid"):
        account = "always_free"
    setting = environ.get("MUSE_IDLE_GUARD", "auto").strip().lower() or "auto"
    if setting in ("off", "0", "false", "no"):
        enabled, why = False, "MUSE_IDLE_GUARD=off"
    elif setting in ("on", "1", "true", "yes"):
        enabled, why = True, "MUSE_IDLE_GUARD=on"
    elif account == "paid":
        enabled, why = False, "paid account (MUSE_ACCOUNT_TYPE=paid, MUSE_IDLE_GUARD=auto)"
    else:
        enabled, why = True, "Always Free account (MUSE_ACCOUNT_TYPE=always_free, MUSE_IDLE_GUARD=auto)"
    try:
        target = float(environ.get("MUSE_IDLE_GUARD_TARGET_PCT", "25"))
    except ValueError:
        target = 25.0
    target = max(ORACLE_IDLE_PCT + 0.5, min(40.0, target))
    return enabled, target, why


def guard_duty(target_pct: float, cpus: int) -> float:
    """Share of one core the guard's single thread runs so the whole machine sits near target_pct."""
    return max(0.0, min(MAX_DUTY, target_pct / 100.0 * cpus))


def guard_due(quiet_minutes: int) -> bool:
    """A guard minute is due when this many minutes in a row stayed under the target by themselves."""
    return quiet_minutes >= GUARD_EVERY_MIN - 1


def others_pct(busy_delta: int, total_delta: int, own_cpu_s: float, cpus: int, elapsed_s: float) -> float:
    """Processor share used by everything except this process over an interval, in % of the machine."""
    if total_delta <= 0 or elapsed_s <= 0:
        return 0.0
    all_pct = 100.0 * busy_delta / total_delta
    own_pct = 100.0 * own_cpu_s / (elapsed_s * cpus)
    return max(0.0, all_pct - own_pct)


class Guard:
    """One low-priority thread that keeps the processor near the target for a set time.

    It stops early when asked, or when other work needs the processor. It
    allocates nothing beyond its own few objects and touches no network.
    """

    def __init__(self, target_pct: float, cpus: int) -> None:
        self.target_pct = target_pct
        self.cpus = cpus
        self.duty = guard_duty(target_pct, cpus)
        self.thread: threading.Thread | None = None
        self.stop = threading.Event()
        self.busy_s = 0.0
        self.backed_off = False

    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def start(self, seconds: float) -> None:
        if self.running() or self.duty <= 0:
            return
        self.stop.clear()
        self.backed_off = False
        self.thread = threading.Thread(target=self._run, args=(seconds,), name="idle-guard", daemon=True)
        self.thread.start()

    def halt(self) -> None:
        self.stop.set()
        if self.thread is not None:
            self.thread.join(timeout=5)

    def _run(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        busy0, total0 = parse_cpu(_read("stat"))
        own0, t0 = time.process_time(), time.monotonic()
        while not self.stop.is_set() and time.monotonic() < end:
            slice_end = time.monotonic() + SLICE_S
            spin_until = time.monotonic() + SLICE_S * self.duty
            while time.monotonic() < spin_until:
                pass
            self.busy_s += SLICE_S * self.duty
            self.stop.wait(max(0.0, slice_end - time.monotonic()))
            if time.monotonic() - t0 >= 1.0:
                busy1, total1 = parse_cpu(_read("stat"))
                own1, t1 = time.process_time(), time.monotonic()
                if others_pct(busy1 - busy0, total1 - total0, own1 - own0, self.cpus, t1 - t0) > BACK_OFF_PCT:
                    self.backed_off = True
                    return
                busy0, total0, own0, t0 = busy1, total1, own1, t1


def daily_line(day: str, enabled: bool, why: str, target: float, stats: dict[str, int]) -> str:
    """The one plain line a day the guard writes to the journal."""
    if not enabled:
        return (f"muse-vm-monitor: idle guard {day}: off ({why}); did nothing. "
                f"Busiest minutes reached {target:g}% by themselves {stats['busy_minutes']} times. "
                "It never holds memory or sends traffic.")
    return (f"muse-vm-monitor: idle guard {day}: on ({why}); ran the processor at about {target:g}% "
            f"for {stats['guard_minutes']} minutes because nothing else reached {target:g}% for "
            f"{GUARD_EVERY_MIN} minutes in a row; real work reached it in {stats['busy_minutes']} minutes; "
            f"backed off for real work {stats['backed_off']} times. It never holds memory or sends traffic.")


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
    enabled, target, why = guard_settings(environ)
    cpus = cpu_count()
    guard = Guard(target, cpus)
    stop = threading.Event()

    def _stop(_signum: int, _frame: object) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    quiet = 0
    day = time.strftime("%Y-%m-%d", time.gmtime())
    stats = {"guard_minutes": 0, "busy_minutes": 0, "backed_off": 0}
    busy0, total0 = parse_cpu(_read("stat"))
    net0 = parse_net(_read("net/dev"))
    t0 = time.time()
    guard_busy0 = 0.0
    if once:
        time.sleep(0.2)
    while not stop.is_set():
        if not once:
            # Wake on the wall-clock minute, so a guard minute covers one whole metric minute.
            deadline = (int(time.time()) // INTERVAL_S + 1) * INTERVAL_S
            if stop.wait(max(0.0, deadline - time.time())):
                break
        now = time.time()
        busy1, total1 = parse_cpu(_read("stat"))
        net1 = parse_net(_read("net/dev"))
        meminfo = parse_meminfo(_read("meminfo"))
        elapsed = max(0.001, now - t0)
        cpu = 100.0 * (busy1 - busy0) / (total1 - total0) if total1 > total0 else 0.0
        bps = max(0.0, (net1 - net0) * 8.0 / elapsed)
        guard_s = guard.busy_s - guard_busy0
        guard_busy0 = guard.busy_s
        if guard.backed_off:
            stats["backed_off"] += 1
            guard.backed_off = False

        # Count the minute. A minute that reached the target with no help from the guard is real work.
        if cpu >= target and guard_s == 0:
            stats["busy_minutes"] += 1
        quiet = 0 if cpu >= target else quiet + 1

        today = time.strftime("%Y-%m-%d", time.gmtime(now))
        if today != day:
            print(daily_line(day, enabled, why, target, stats), flush=True)
            day, stats = today, {"guard_minutes": 0, "busy_minutes": 0, "backed_off": 0}

        if enabled and not once and guard_due(quiet) and not guard.running():
            # From this minute's start to a few seconds past its end.
            guard.start(INTERVAL_S + GUARD_MARGIN_S)
            stats["guard_minutes"] += 1

        mem = memory_used_pct(meminfo)
        sample = {"t": round(now), "cpu": round(cpu, 2), "mem": round(mem, 2), "net_bps": round(bps), "guard_s": round(guard_s, 1)}
        try:
            append_sample(directory, sample)
            prune_samples(directory, now)
            write_guard_state(directory, {
                "enabled": enabled,
                "why": why,
                "uses": "processor only; never memory or network",
                "target_pct": target,
                "oracle_idle_threshold_pct": ORACLE_IDLE_PCT,
                "one_minute_in": GUARD_EVERY_MIN,
                "minutes_since_target_reached": quiet,
                "running_now": guard.running(),
                "today": dict(stats),
                "cpu_pct_last_minute": round(cpu, 2),
                "metric": "CpuUtilization (namespace oci_computeagent)",
                "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
            })
        except OSError as exc:
            print(f"muse-vm-monitor: could not write a sample: {exc}", file=sys.stderr)
        busy0, total0, net0, t0 = busy1, total1, net1, now
        if once:
            break
    guard.halt()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
