
# Lean verification

The useful-work runner can verify a proof in two modes, set by
`MUSE_LEAN_MODE`.

## Mock (default, used in CI)

`MUSE_LEAN_MODE=mock` does not run Lean. It:

- rejects any proof that still contains `sorry` or `admit`;
- accepts a small set of known-good tactics for the bundled samples
  (`sample.nat_add_comm`, `sample.nat_add_zero`);
- rejects everything else, including formal-conjectures problems.

This is conservative on purpose: a hallucinated proof must not receive
a "verified" hash. The mock exists so CI and a laptop without Lean
can still exercise the job runner, the result hash, and the x402 sale
path.

Bundled samples live in `muse/useful_work/samples/`. They are original
tiny `Nat` theorems, not copies of
[google-deepmind/formal-conjectures](https://github.com/google-deepmind/formal-conjectures).

## Real Lean (optional, local)

1. Install [elan](https://github.com/leanprover/elan), which is the
   Lean 4 toolchain manager:

   ```bash
   curl https://raw.githubusercontent.com/leanprover/elan/master/elan-init.sh -sSf | sh
   # then restart the shell, or source the env file it prints
   lean --version
   ```

2. Set `MUSE_LEAN_MODE=real`. The verifier writes a temporary `.lean`
   file and runs `lean <file>`. Exit code 0 and no `sorry` in the proof
   counts as verified. A missing binary returns `verdict: error` and
   tells you to install elan.

3. A full Lake project, mathlib, and formal-conjectures' own Lake
   configuration are **not** wired. Real mode is enough to check the
   bundled samples and other self-contained theorems that only need
   prelude `Nat`. formal-conjectures problems generally need mathlib;
   treat that as a later step (a Lake workspace that depends on a
   pinned formal-conjectures commit).

## formal-conjectures

The loader will pick up `.lean` files that still contain `sorry` if
you set `MUSE_FORMAL_CONJECTURES` to a local clone:

```bash
git clone --depth 1 https://github.com/google-deepmind/formal-conjectures.git /path/to/formal-conjectures
export MUSE_FORMAL_CONJECTURES=/path/to/formal-conjectures
```

Those problems are listed on `GET /v1/work/problems`. In mock mode
their proofs are not accepted (there is no oracle). In real mode they
will only verify if your Lean environment can see their dependencies,
which phase 0 does not install.

Do not vendor formal-conjectures in this repository: it is large and
separately licensed.
