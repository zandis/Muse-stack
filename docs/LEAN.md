# Lean verification

The useful-work runner can verify a proof in two modes, set by
`MUSE_LEAN_MODE`. Both modes use the same *lake-comparator-style*
front end: the trusted statement comes from the problem file, the
model's proof is not allowed to replace that theorem, and banned
constructs are rejected before Lean (or the mock) says anything
about the kernel.

## Statement lock (always, mock and real)

`muse.useful_work.comparator.assemble`:

1. Reads the trusted theorem from the problem file (preamble, imports,
   namespace, and the statement including the `sorry` hole).
2. If the model returns a full `theorem` / `lemma`, its *signature*
   is compared (whitespace- and comment-insensitive) to the trusted
   one. A mismatch — for example `theorem x : True := trivial` against
   a `Nat` commutativity problem — is rejected. The model's theorem
   declaration is never written as the file.
3. Only the target hole is filled. The assembled file is: trusted
   preamble + trusted theorem with the proof body, then a
   `#print axioms` / `#check` appendix in real mode.
4. A comment-aware scan (`muse.useful_work.banned`) rejects
   `sorry`, `admit`, `native_decide`, `ofReduceBool`,
   `implemented_by`, `debug.skipKernelTC`, `extern`, axiom
   declarations, and elaboration-time IO (`#eval`, `#run`, …).
   `sorry` inside a comment or string does not count.

There is no `lake comparator` binary in CI. This is the equivalent
fail-closed check, and it is what runs even when Lean is absent.

## Mock (default, used in CI)

`MUSE_LEAN_MODE=mock` does not run Lean. After the statement lock and
the banned-token scan, it **always** returns

```
verified: false
mode: mock
```

even when the static checks passed. Mock results are **never listed
for sale** and never receive a "verified" hash. The mock does **not**
accept a proof merely because the text mentions a known-good tactic
(`Nat.add_comm`, `rfl`, `trivial`, …).

This is conservative on purpose: a hallucinated proof, or a proof that
only looks like a tactic name, must not become a paid good. The mock
exists so CI and a laptop without Lean can still exercise the job
runner and the HTTP surface. To sell a result in tests, inject a
record that was marked verified by a *real* Lean path (or a test
double of one).

Bundled samples live in `muse/useful_work/samples/`. They are original
tiny `Nat` theorems, not copies of
[google-deepmind/formal-conjectures](https://github.com/google-deepmind/formal-conjectures).
`tests/fixtures/formal_conjectures/` covers the loader against a
realistic shape (imports, module comments, a `sorry` only inside a
comment, unique `{path}#{theorem}` ids). `.lake` / `.git` / `build`
trees are skipped.

## Real Lean (optional, local)

1. Install [elan](https://github.com/leanprover/elan), which is the
   Lean 4 toolchain manager:

   ```bash
   curl https://raw.githubusercontent.com/leanprover/elan/master/elan-init.sh -sSf | sh
   # then restart the shell, or source the env file it prints
   lean --version
   ```

2. Set `MUSE_LEAN_MODE=real`. The verifier writes a temporary `.lean`
   file (the *assembled* trusted statement, never the model's theorem)
   and runs `lean` **in a sandbox**:

   - Environment is scrubbed: no `*_API_KEY`, `*_MASTER_KEY`,
     `*_SECRET`, `*_TOKEN`, or similar, and no leftover provider
     credentials.
   - Network is disabled when [bubblewrap](https://github.com/containers/bubblewrap)
     is on `PATH` (`bwrap --unshare-net --unshare-pid --die-with-parent
     --clearenv`, with tight ro-binds). A temporary working directory
     is the only writable location.
   - CPU, address-space, file-size, and process rlimits are applied,
     plus a wall-clock timeout.

   **Production path.** Bubblewrap on a shared host is a reasonable
   minimum. A deployment that accepts untrusted proofs should run the
   same job inside [gVisor](https://gvisor.dev/) or
   [Firecracker](https://firecracker-microvm.github.io/) (or an
   equivalent VM / syscall jail). This repository documents those
   options; it does not vendor a gVisor rootfs in CI.

3. Exit code 0 is **not** enough. The verifier then:

   - Parses `#print axioms`. If the section is missing or unparseable,
     the result is fail-closed (`verified: false`).
   - Allows only `propext`, `Quot.sound`, and `Classical.choice`.
     Anything else (including axioms introduced by `sorry` /
     `native_decide`) is rejected.
   - Checks that `#check` mentions the trusted constant, so a file
     that happens to type-check a *different* theorem is not sold as
     this problem.

A missing `lean` binary returns `verdict: error`,
`verifier: lean4-unavailable`, and `verified: false`. It does not
fall back to the mock's "accept" path, because the mock does not
accept either.

## formal-conjectures

google-deepmind/formal-conjectures is not vendored. Point
`MUSE_FORMAL_CONJECTURES_DIR` at a local checkout. The loader:

- skips `.lake`, `.git`, and `build`;
- treats `sorry` inside comments or strings as not a hole;
- keeps imports and the surrounding namespace as the preamble;
- assigns unique ids `{relative/path.lean}#{theorem_name}`.
