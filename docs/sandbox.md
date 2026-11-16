# The skill sandbox

Work package A12. A skill's code runs in a separate, locked-down process. The parent keeps everything that matters: the router client and its app token, the budget, the privacy class, the retry on a failed check, and the decision to write a file.

**This has not had an outside security review.** It is tested against the escape attempts listed below on one Linux machine, by its author. Treat it as a strong seat belt. Third-party skills should not be allowed until someone outside the project has tried to break it (gate G3 books that review).

```bash
muse doctor                                                    # what this computer can enforce
muse skills run files-answer --folder ~/notes --sandbox require --input question="..."
MUSE_SANDBOX=auto muse skills run files-answer --folder ~/notes --input question="..."
python -m pytest tests/test_sandbox_escape.py -q -rs           # the escape attempts, for real, on this machine
```

## 1. Threat model

The code being contained is a skill: Python that the person installed, or (later) an MCP tool. Assume it is hostile, or that text in a file it read talked a model into misusing it. It wants to:

1. read secrets: provider keys, Wardmuse's app tokens, the vault, the ledger, the approval list, anything in `MUSE_HOME`, the environment of Wardmuse's own process;
2. read or change files the person did not name for this run;
3. send what it read somewhere (any network path), or fetch instructions;
4. spend the person's allowance or money beyond the job's budget, or send under a looser privacy class;
5. write files without the person's agreement;
6. outlive the job, or harm the machine: exhaust memory, disk, processes, processor time;
7. interfere with other processes: signal them, trace them, read their memory;
8. lie to the parent so that the result, the records or the screen say something untrue.

Out of scope: a kernel bug; an attacker who already runs as the person outside the sandbox; a malicious Python runtime or `muse` package (they are the trusted base); what a skill does with data it was legitimately given, inside the answer it returns (the answer goes to the person; the confirm card is the control for writes). Timing and other side channels are not addressed.

## 2. Design

```
  parent (muse, as the person)                      child (one per run)
  ─────────────────────────────                     ─────────────────────────────────────────
  loader: the skill's hashed bytes ──── load ─────▶ imports the skill from those bytes
  runner: budget, privacy, retry                    Context: inputs, workspace, data_dir
  router client + app token  ◀── ask / embed ────── ctx.llm (a proxy: no token, no network)
  Workspace re-checks        ◀── attempt_done ───── output, notes, proposed writes (as data)
  kills the process group on timeout                locked down BEFORE any skill code runs
```

**One process per run.** `python -I -B` (isolated mode: no `PYTHON*` variables, no user site, no current folder on the import path) started with:

- an environment built from an allow-list: a fixed `PATH=/usr/bin:/bin`, `HOME` and `TMPDIR` in a private temp folder, and `LANG`, `LC_ALL`, `TZ`, `MUSE_TZ` copied when set. Nothing else: no provider key, no `MUSE_HOME`, no app token;
- no open descriptors except stdio (all `/dev/null`) and the two ends of the control pipe. The child closes everything else again itself;
- its own session and process group, and `PR_SET_PDEATHSIG` so it dies with the parent.

**Order in the child** (`muse/sandbox/child.py`): close descriptors, import Wardmuse's own modules, lock down, report what was applied, and only then receive and import the skill. No skill code, not even a module's top level, runs before the lock-down. The skill is imported from the bytes the parent hashed, sent over the pipe; its folder is never read in the child.

**The lock-down** (`muse/sandbox/lockdown.py`, standard library and `ctypes` only):

| Step | What it does |
|---|---|
| `setrlimit` | address space, processor time, open files, processes, largest file, no core dumps |
| `PR_SET_NO_NEW_PRIVS` | no `exec` can gain privileges (setuid, file capabilities). Needed by the next two |
| drop capabilities | empties every capability set. Matters only if Wardmuse runs as root |
| Landlock | deny by default; read the runtime and the named folders; read and write `data` and temp; no execute right anywhere |
| seccomp | a small filter: no new sockets, no `io_uring`, no ptrace, no namespaces or mounts, signals only to itself |

**Layers**, strongest first. Each is optional, each is tried for real by `muse doctor`, and each run reports which were active:

| Layer | Files | Network | Processes |
|---|---|---|---|
| **bubblewrap** (`bwrap --unshare-all --die-with-parent --new-session`) | A root that holds only: `/usr`, `/lib*`, `/bin` read-only; the Python runtime and the `muse` package read-only; each named folder read-only; `data` read-write; a size-capped tmpfs for temp. The root itself is read-only. No `/proc`, no `/sys`, no `/home`, a minimal `/dev`. | A new, empty network namespace | A new process namespace: outside processes do not exist; killing bubblewrap kills everything inside |
| **Landlock** (applied by the child to itself; works without namespaces) | The same path list as rules. Paths not listed are denied whatever the file permissions say. Symbolic links are judged by where they lead. No file may be executed. | ABI 4 and later: TCP bind and connect denied. **UDP and unix sockets are not covered** at the ABIs used | ABI 6 and later: signals only within the sandbox; abstract unix sockets scoped. ptrace of outside processes denied at every ABI |
| **seccomp** | — | No socket of any family can be created; `io_uring` refused | No ptrace, `process_vm_*`, `pidfd_*`; no `setsid`/`setpgid` (so it cannot leave the group the parent kills); no `unshare`/`setns`/mount calls; `kill` only with its own pid |
| **`Workspace` checks** (unchanged, in the child and again in the parent) | Every path a well-behaved skill uses is resolved and checked, as before | — | — |

**Network: none.** The skill still reaches a model, without any network: `ctx.llm` in the child is a proxy that sends the request over the control pipe; the parent's router client makes the call with its own token and returns the answer. Budget is counted and privacy applied in the parent exactly as in the in-process runner, by the same code. Skills that declare `net` hosts stay refused (section 8).

**Reads: bound folders, not mediated.** The named folders are shared read-only (bubblewrap) and allowed read-only (Landlock); the skill reads them directly through the same `Workspace` as before. The alternative, every read as a request to the parent, gives per-file control and a receipt per file, but the shipped skills index up to 2,000 files and keep a SQLite database, and would pay a round trip per read. Bound folders keep `Workspace` semantics identical and the speed; the permission boundary is the folder, which is also what the person approved ("this folder"). Per-file receipts can be added later by having the child report what it opened; that report would be a claim by untrusted code, so a mediated mode remains the answer where proof is needed.

**PDF parsing happens in the child.** A PDF from the person's folders is hostile input. The files skills parse it where they read it, in the skill (`skills/*/pdf.py`, with the optional pypdf), so under the sandbox the parser runs after the lock-down, with no network, no keys and the child's limits, and a parser bug reaches only what the child can already read. The parent never parses a PDF; the extracted text reaches it only inside the skill's output, like any other passage. The alternative, parsing in the parent and handing text to the child, would put a parser of untrusted files next to the router client and its token. pypdf is pure Python and needs no thread, process or network; the skill tightens its decoding limits. A test (`tests/test_sandbox.py::test_pdfs_are_read_the_same_way_sandboxed_and_hostile_ones_end_no_run`) runs the same PDFs, including a truncated file and a stream that inflates past the limit, in process and sandboxed, and requires identical results. With the sandbox off, the parser runs in Wardmuse's own process like the rest of the skill.

**Writes are still proposals.** The output folder is not shared with the child at all. `ctx.workspace.propose_write` records the file in the child; the proposals travel back as data with the result; the parent's own `Workspace` checks every path again and applies nothing without consent. A child that reports a path outside the output folder fails the run.

**The control pipe** (`muse/sandbox/protocol.py`): frames of a 4-byte length and UTF-8 JSON, one object with an `op`.

| Direction | Frames |
|---|---|
| parent → child | `init` (limits, path lists), `load` (the skill's bytes), `attempt`, `check`, `reply`, `exit` |
| child → parent | `ready` (what was applied), `loaded`, `attempt_done`, `check_done`; requests `ask`, `embed`, `remaining`; `event` |

The parent treats every byte as hostile: a frame longer than the cap is refused before it is read; malformed JSON, an unknown or out-of-turn `op`, or a wrong shape ends the run; reads and writes have a deadline; events must be `skill.*` with numbers and short codes, and are capped in number. An error the child reports is looked up among the errors the parent itself raised: a child cannot invent a "the router deferred this" outcome. Text from the child reaches the person in exactly three places, each one already allowed for a skill: its output, its notes, and the sentence of `ctx.stop(...)`. A crash arrives as a type name and `file.py:line`, both checked against a pattern.

**Resources.** In the child: `RLIMIT_AS` 2 GiB, `RLIMIT_CPU` the job's `seconds` plus the grace, `RLIMIT_NOFILE` 256, `RLIMIT_NPROC` 64, `RLIMIT_FSIZE` 1 GiB, no core files. In the parent: a wall-clock deadline of the job's `seconds` plus 15 s, after which the whole process group is killed; the `data` folder (and the temp folder when it is not a tmpfs) measured every second and the run ended above 4 GiB (256 MiB for temp); message size 64 MiB. All in `muse.sandbox.Policy`.

**Result.** The same `Result` as the in-process runner, because it is built by the same runner code; only the place where the skill's two functions run differs. `Result.sandbox` carries the report. A crash, timeout, cap or broken conversation is `failed` with one plain sentence ("Stopped: the skill ran past its time budget and was ended.").

## 3. Modes and the rule for skills of your own

`MUSE_SANDBOX=off|auto|require`, or `--sandbox` on `muse skills run`, or `sandbox=` on `muse.skills.run`.

| Mode | Meaning |
|---|---|
| `off` (default, for now) | Shipped skills run in Wardmuse's own process, as before |
| `auto` | Use every layer this computer has; run even if some are missing; say exactly which were active |
| `require` | Refuse to run unless files **and** network are both isolated |

- **A skill that does not ship with Wardmuse always runs with `require`**, whatever the setting or the flag says. Where the sandbox is incomplete, it is refused with the reason. This is the rule of the ecosystem review: third-party code is untrusted code. There is one way around it, for Wardmuse's own test suite only: the process environment variable `MUSE_SANDBOX_UNSAFE_HOME_SKILLS=in-process-for-tests`, set by `tests/conftest.py` because older tests exercise the runner with throw-away skills. Never set it on a real home.
- Shipped skills follow the setting. The default stays `off` until the escape suite has passed on the owner's machines; then it should become `auto`.
- "Files isolated" means bubblewrap, or Landlock at ABI 3 or later (before ABI 3 a readable file can still be emptied). "Network blocked" means a network namespace, or the seccomp filter. Landlock's TCP rules alone are reported as `net: tcp blocked` and do **not** satisfy `require`.
- Two shipped skills, `setup-services` and `recurring-chore`, read Wardmuse's own state (the catalogue, which services have a key, the chore list) through Wardmuse's modules. The sandbox hides that state, so under `auto` they run in-process and the report says so; under `require` they are refused. They need a mediated way to ask for those facts before they can be contained.
- A run is refused when a named folder is `/` or contains `MUSE_HOME`, or when the Python runtime lives somewhere that cannot be shared safely.

## 4. The report

```python
result, report = muse.sandbox.run_skill("files-answer", {"question": "..."}, roots=["~/notes"], mode="auto")
report.isolated    # True only when files AND network were both isolated
report.layers()    # ['bwrap', 'landlock: abi 7', 'seccomp', 'no_new_privs', 'rlimits', 'files: isolated', 'net: blocked']
report.best_effort # what a layer could not do here, in words; never silently empty
report.sentence()  # "Ran isolated: bwrap, landlock: abi 7, ..." | "Ran in a separate process, NOT fully isolated: ..."
```

The report is filled from what the child said it applied, in a frame sent after the lock-down and before any skill code exists in that process. `muse skills run` prints the sentence; `--json` includes the whole report; the run's events include one `sandbox` event for records. When the sandbox was not involved at all, nothing is claimed either way.

`muse.sandbox.probe()` returns `Capabilities`; `muse doctor` prints it. Nothing is inferred from version numbers: bubblewrap counts only if a Python child really came up inside it with the options a run uses, Landlock only if a read was really denied, seccomp only if a socket was really refused.

For debugging only: `MUSE_SANDBOX_DEBUG=1` in Wardmuse's own environment lets the child's standard error through to the terminal (normally it goes nowhere). A skill can then print whatever it read, so do not leave it set.

`run_skill(..., token=..., policy=Policy(authorize=fn))`: an optional permission token is accepted and handed, with the action (`llm.ask`, `llm.embed`, `files.propose`) and a few facts, to `authorize` in the parent before each thing done on the skill's behalf. Nothing checks tokens here; the permission compiler plugs in at that point.

## 5. Platform matrix

| Platform | State |
|---|---|
| Linux x86_64 | Built and tested by the author: bubblewrap 0.9.0, Landlock ABI 7, seccomp, kernel 6.18, in a container, once as root and once as an unprivileged user (`nobody`) |
| Linux aarch64 (the owner's VM) | Same code. **Not run by the author.** The seccomp syscall numbers for aarch64 come from the kernel's generic table and are checked at start-up by a self-test (a socket must be refused, or the layer is reported as not working). Run the suite there: section 6 |
| macOS | `sandbox-exec` with a generated profile (`muse/sandbox/launch.py::macos_profile`): deny by default, the same paths, no network. **UNTESTED**: written without a Mac. It is used only if its self-test passes on the machine, never counts as isolation, and `require` refuses on macOS. Apple marks `sandbox-exec` deprecated |
| Windows | Nothing. `probe()` says no isolation is available; `auto` runs in-process and says so; `require` refuses. Work package A13 |

## 6. Running the escape suite on another machine

On the owner's VM (Ubuntu 24.04, aarch64):

```bash
sudo apt-get install -y bubblewrap
cd Wardmuse-stack && python -m pip install -e . && python -m pip install --require-hashes -r requirements/dev.lock
python -m muse doctor                                              # look for the "Skill sandbox" line
python -m pytest tests/test_sandbox_escape.py -q -rs               # the escape attempts; -rs prints every skip and its reason
python -m pytest tests/test_sandbox.py -q -rs                      # the rest: pipe, limits, parity, modes
```

Run it as an ordinary user, not root: the process-count limit is only testable that way, and it is how Wardmuse should run.

Ubuntu 24.04 restricts unprivileged user namespaces through AppArmor. If `muse doctor` says bubblewrap is not usable with a message such as `setting up uid map: Permission denied`, bubblewrap needs an AppArmor profile that allows `userns` (Ubuntu ships one as `/usr/share/apparmor/extra-profiles/bwrap-userns-restrict`; copy it to `/etc/apparmor.d/` and reload AppArmor), or the restriction must be lifted with `sysctl kernel.apparmor_restrict_unprivileged_userns=0`, which weakens the system for every program. Without bubblewrap the sandbox still reaches `require` with Landlock plus seccomp on that kernel, and the suite runs those layers.

A test is skipped, with the reason, when the layer it needs cannot run on the machine. Nothing is faked: `test_without_any_layer_the_same_attempts_succeed` shows that the hostile skill really does these things when no layer is on.

## 7. What was tested, and what each layer stopped

On the author's machine (Linux x86_64, all three layers available; the suite was run as root and again as `nobody`), each attempt ran against each layer by itself and against all together. "✓" = the attempt failed and the test checked the trace outside the sandbox; "—" = that layer is not meant to stop it.

| Attempt by the hostile skill | bubblewrap | Landlock | seccomp |
|---|---|---|---|
| Read a file outside the named folders; `/etc/passwd`; list a folder | ✓ | ✓ | — |
| Read `MUSE_HOME` (a canary, the app list, the approval list, the key file) | ✓ | ✓ | — |
| Write outside `data`: other folders, the named (read-only) folder, `MUSE_HOME`, the output folder, `/` | ✓ | ✓ | — |
| Follow a symbolic link out (file, folder, into `MUSE_HOME`); plant one in `data` | ✓ | ✓ | — |
| Read another process's environment or memory through `/proc` | ✓ (no `/proc`) | ✓ | **not stopped** (it filters calls, not paths) |
| Attach to another process with ptrace | ✓ | ✓ | ✓ |
| Open a TCP connection to a local listener | ✓ | ✓ (ABI 4) | ✓ |
| Send UDP | ✓ | **not stopped** | ✓ |
| Connect to a unix socket outside the named folders | ✓ | **not stopped** | ✓ |
| Connect to a unix socket **inside** a named folder | **not stopped** | **not stopped** | ✓ |
| Spawn a shell | runs, but inside the same small world | ✓ (no execute right) | — |
| Kill a process outside; signal its parent | ✓ | ✓ (ABI 6) | ✓ |
| Leave a process running after the job | ✓ | **not stopped** | ✓ |
| One huge file in `data` | ✓ `RLIMIT_FSIZE` (all layers) | | |
| Many files in `data` past the cap | ✓ ended by the parent's watch (all layers; bounded overshoot) | | |
| Fill the temp folder | ✓ tmpfs size | ✓ parent's watch | — |
| Allocate past the memory cap; spin past the time budget | ✓ `RLIMIT_AS`; wall-clock kill (all layers, and with none) | | |
| Fork bomb | ✓ `RLIMIT_NPROC`, when run as an unprivileged user. As root the test is skipped: the kernel exempts root | | |
| See a secret environment variable; inherit an open file | ✓ scrubbed environment, closed descriptors (all layers, and with none) | | |
| Garbage, oversized frames, early exit, forged events, a lie about a write, content in an error | ✓ the parent's protocol checks (all layers, and with none) | | |

## 8. What remains unprotected

Be specific, so nobody assumes more than there is.

- **No outside review.** The filter, the rules and the protocol were written and tested by one author.
- **Landlock alone is not a network block**: UDP and unix sockets pass. `require` therefore never accepts Landlock without seccomp or bubblewrap. Newer Landlock ABIs add UDP and unix-socket rules; they are not used yet because their constants could not be verified here.
- **bubblewrap alone reaches a unix socket that lies inside a named folder** (the socket is shared with the folder). seccomp closes this. On a machine with bubblewrap and no seccomp, do not name a folder that holds a service socket; the report carries a line about it.
- **Disk.** The total size of `data` is enforced by a watch in the parent, about once a second, so a fast writer overshoots before it is stopped (by up to roughly a second of writing). A filesystem quota or a size-limited mount for `data` would be exact; neither is used. `RLIMIT_FSIZE` is exact per file.
- **Memory and processor.** `RLIMIT_AS` and `RLIMIT_CPU` are per process, not per job: a skill that forks multiplies them by up to the process limit. cgroup limits would be exact; cgroup v2 delegation is not used (it was not available where this was built).
- **The process limit is not enforced when Wardmuse runs as root** (the kernel exempts root), and outside a user namespace it counts all of the person's processes, so a skill may find it cannot start any thread. Skills must not rely on threads or subprocesses. Do not run Wardmuse as root.
- **Without bubblewrap and without seccomp**, a process the skill starts in a new session survives the job. Reported per run.
- **seccomp is a deny-list**, not an allow-list: it closes the calls listed in `muse/sandbox/lockdown.py` and leaves the rest of the kernel's surface open. It does not inspect `clone` flags; creating a user namespace that way is not blocked (Landlock and the missing capabilities still apply inside it). The aarch64 numbers were not run by the author.
- **`stat` and similar metadata calls are not restricted by Landlock**: under Landlock alone a skill can learn that a path exists, its size and times, though not its content or a folder's listing. bubblewrap hides the paths entirely.
- **The system folders are readable**: `/usr`, `/lib*`, `/bin`, the Python runtime and Wardmuse's own program files. They hold programs, not the person's data; anything private kept under `/usr` would be visible.
- **PDF parsing is only contained when the sandbox is on.** With `off` (the default for shipped skills for now) pypdf runs in Wardmuse's process; it is pure Python, so a hostile PDF can raise, loop or allocate within the skill's limits but not corrupt memory. The time limit per PDF is checked between pages, so one pathological page is bounded only by the job's time (and, sandboxed, by `RLIMIT_CPU` and the wall-clock kill).
- **What the skill may read, it may copy** into its `data` folder or return in its answer. The sandbox does not judge content. `data` persists between runs of the same skill.
- **The control pipe is an attack surface.** JSON parsing of hostile input happens in the parent. Frames are size-capped and shape-checked, and fuzzed by the tests, but the parser is the standard library's.
- **Timing**: the wall-clock check does not interrupt the parent while it waits on the router for the skill (that wait is bounded by the budget's seconds).
- **Shipped skills that use Wardmuse's modules are not contained** (section 3). Skills with `net` hosts cannot run.
- **macOS and Windows**: no tested isolation.
- **Tokens are not verified by the sandbox yet.** The architecture says the sandbox verifies the permission token itself; today it only passes the token to a hook. The compiler package wires that.

## 9. Later

- **An egress allow-list for `net` hosts.** The child never gets a network. A skill that declares hosts would get a `ctx.http` proxy (or, for tools that need a socket, a unix socket to a forward proxy run by the parent), and the parent would make the request only to the declared hosts, over TLS, counting bytes, with the permission token naming the hosts. Same shape as `ctx.llm`: the capability stays in the parent.
- Mediated file reads with a receipt per file, for permissions narrower than a folder.
- cgroup v2 limits for memory, processes and processor time per job; a quota for `data`.
- A seccomp allow-list; Landlock's UDP and unix-socket rules on kernels that have them.
- MCP tools through the same `Executor`: one process per tool call, the tool's own service token passed in over the pipe rather than the environment.
- A tested macOS profile; Windows (A13): a restricted token and a job object.
