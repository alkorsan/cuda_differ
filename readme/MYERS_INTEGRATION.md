# Myers diff integration — change summary

This document explains every change made to integrate `MyersSequenceMatcher`
(from Meld's `myers.py`) into the Differ plugin, and why.

## Goal

- Integrate the attached `myers.py` (Meld's O(NP) Wu/Manber/Myers/Miller
  1989 implementation with common prefix/suffix trimming and a
  non-matching-line discard preprocessing pass) as a new diff algorithm.
- Make `myers` the default algorithm.
- Keep `vscode`, `patience`, and `difflib` as alternatives (no backward-
  compatibility shims needed since the plugin was never published).
- Make the minimum necessary change to `myers.py` so future upstream
  merges stay trivial.

## Files modified

### 1. `myers.py` — minimal one-call change + two state-reset lines

**Change 1 (super().__init__):** added one `super().__init__(...)` call at
the start of `MyersSequenceMatcher.__init__`:

```python
super().__init__(isjunk=None, a="", b="", autojunk=False)
```

**Why this change is needed:**

The upstream `MyersSequenceMatcher.__init__` does NOT call
`super().__init__()`. Meld only ever uses Myers for the main line-level
diff (via `MyersSequenceMatcher(isjunk, a, b)` followed by
`get_opcodes()`), and Myers' own `initialise()` / `build_matching_blocks()`
code path doesn't need the inherited difflib attributes. So in Meld, the
missing `super().__init__()` is harmless.

The Differ plugin, however, also reuses the matcher for **character-level
detail diffing** inside a 'replace' block — see `Differ._fancy_replace` in
`differ.py`:

```python
diff = InlineMyersSequenceMatcher(None)
diff.set_seq2(bj)              # <-- inherited from difflib
diff.set_seq1(ai)              # <-- inherited from difflib
diff.real_quick_ratio()        # <-- inherited from difflib
diff.quick_ratio()             # <-- inherited from difflib
diff.ratio()                   # <-- inherited from difflib
diff.get_opcodes()             # <-- overridden by Myers
```

`set_seq2()` calls difflib's `__chain_b()`, which uses `self.isjunk` and
`self.autojunk`. Without `super().__init__()`, those attributes are never
set, so the first `set_seq2()` raises `AttributeError: 'MyersSequenceMatcher'
object has no attribute 'isjunk'`. (Confirmed by direct test before the
fix.)

The fix initializes all difflib attributes properly. `autojunk=False` is
passed explicitly because Myers does not implement the autojunk heuristic
and must not silently apply it inside the inherited `__chain_b` /
`quick_ratio` code paths.

**Change 2 (lines_discarded reset):** added `self.lines_discarded = False`
to the early-return paths in BOTH `preprocess_discard_nonmatching_lines`
methods (the base `MyersSequenceMatcher` and `InlineMyersSequenceMatcher`):

```python
if len(a) == 0 or len(b) == 0:        # base class
    self.aindex = []
    self.bindex = []
    self.lines_discarded = False       # <-- added
    return (a, b)

if len(a) <= 2 and len(b) <= 2:        # InlineMyersSequenceMatcher
    self.aindex = []
    self.bindex = []
    self.lines_discarded = False       # <-- added
    return (a, b)
```

**Why this change is needed:**

Both early-return paths reset `aindex`/`bindex` to `[]` but did NOT reset
`lines_discarded`. When `_fancy_replace` reuses the matcher via
`set_seq1`/`set_seq2` (one matcher per replace block, reused for every
line pair), the following sequence triggers an `IndexError`:

1. First line pair: long, similar lines (e.g. long HTML lines with
   scattered differences) → k-mer preprocessing activates →
   `lines_discarded = True`, `aindex` populated.
2. Later line pair: two lines that, after common prefix/suffix trimming,
   are both ≤ 2 chars with at least one matching char (e.g. two HTML
   lines that differ only in a 2-char middle that's a permutation:
   `<span>ab</span>` vs `<span>ba</span>`) → early return →
   `aindex = []`, but `lines_discarded` STAYS `True` from step 1.
3. `build_matching_blocks` enters the `lines_discarded` branch (because
   `lines_discarded` is True) and tries `aindex[x]` on an empty list →
   `IndexError: list index out of range`.

This is a **latent bug in the upstream Meld code** — it doesn't manifest
in Meld because Meld creates a fresh matcher per diff call and never
reuses via `set_seq1`/`set_seq2`. The differ plugin's matcher reuse in
`_fancy_replace` exposes it. The fix ensures `lines_discarded` is always
consistent with `aindex`/`bindex`.

Both changes are otherwise no-ops for the main line-level diff path
(which Myers owns end-to-end) and are fully compatible with future
upstream merges — if upstream ever adds its own `super().__init__()`
and/or `lines_discarded` resets, you can drop these lines.

### 2. `differ.py` — three small additions

- Added `from .myers import MyersSequenceMatcher, InlineMyersSequenceMatcher`
  to the imports.
- Changed `Differ.__init__`'s `self.diff_algorithm = 'vscode'` to
  `self.diff_algorithm = 'myers'`.
- Added a `myers` branch in `Differ.compare()` (uses
  `MyersSequenceMatcher(None, self.a, self.b)` for the line-level diff).
- Added a `myers` branch in `Differ._fancy_replace()` that uses
  **`InlineMyersSequenceMatcher(None)`** (not `MyersSequenceMatcher`) for
  the character-level detail diff. `InlineMyersSequenceMatcher` is the
  Myers variant Meld designed specifically for character-level (inline)
  diffing — its preprocessing pass uses 3-element k-mers instead of
  single elements, which makes the preprocessing effective on character
  sequences (where single characters are rarely unique). See the comment
  in `_fancy_replace` for the full rationale. Benchmarks show it is 2-4×
  faster than `MyersSequenceMatcher` on medium/long lines for
  character-level diffing, and produces more meaningful character-level
  diffs. The main line-level diff path still uses `MyersSequenceMatcher`
  because lines are usually unique enough that 1-element preprocessing
  is appropriate.
- Updated the algorithm-selection comment.

### 3. `__init__.py` — config UI changes

- Added `myers` as the first entry in the `diff_algorithm` dropdown
  (`OPTS_META`), with the label "Myers (O(NP), fastest)".
- Changed the default from `'vscode'` to `'myers'` in both `OPTS_META`
  and `Command.get_config()`.
- Updated the option's comment to describe Myers and mention that it's
  the new default.
- Updated the `autojunk` option's comment to mention that
  `MyersSequenceMatcher` (in addition to `PatienceSequenceMatcher` and
  `VSCodeSequenceMatcher`) does not support autojunk.

### 4. `readme/readme.txt` — user-facing doc

- Added a `myers` bullet to the "Diff algorithm" section, describing the
  algorithm and its preprocessing optimizations.
- Updated the section to mention "Four choices" instead of "Three".
- Changed the "Default:" line from `vscode` to `myers`.
- Updated the autojunk section to mention `MyersSequenceMatcher`.

### 5. `readme/history.txt` — changelog

- Added a new dated entry describing the integration, the minimal change
  to `myers.py` and why it was needed, and the change of default.

## Why Myers as the default

Benchmarks (see `scripts/bench_myers.py`) on representative inputs:

| input                       | myers | vscode  | patience | difflib |
|-----------------------------|------:|--------:|---------:|--------:|
| similar_small (200 lines)   | 0.12  | 29.36   | 0.20     | 0.24    |
| similar_med (1500 lines)    | 1.12  | 24.14   | 2.09     | 3.70    |
| dupes (1000 lines)          | 0.59  | 3.40    | 0.53     | 0.35    |
| dupes_big (4800 lines)      | 2.98  | 68.62   | 2.59     | 1.68    |
| random_small (200 lines)    | 0.04  | 29.21   | 0.08     | 0.08    |
| random_med (800 lines)      | 0.16  | 578.21  | 0.39     | 0.37    |
| large_realistic (2000)      | 0.54  | 84.60   | 2.43     | 1.76    |
| realistic source 500        | 0.91  | (n/a)   | 1.15     | 1.38    |
| realistic source 1500       | 2.49  | (n/a)   | 3.50     | 4.94    |
| realistic source 3000       | 5.15  | (n/a)   | 7.13     | 11.38   |
| large file 5000             | 9.66  | (n/a)   | 17.04    | 223.96  |

(all times in milliseconds, best of 3 runs)

Myers is the fastest in almost every scenario:
- 10-50x faster than VS Code's DP algorithm (which is O(MN))
- 1.5-3x faster than patience and difflib on medium/large similar files
- Fastest on realistic source-code diffs (small replace blocks)

VS Code's DP algorithm is kept as an alternative for the rare case where
its equality scoring produces better alignment on files with many
duplicated lines (where patience/difflib can produce a giant misaligned
'replace' block).

## End-to-end verification

`scripts/test_integration.py` runs 11 representative diff scenarios
through `Differ.compare()` with all four algorithms and confirms:

- All four algorithms produce **identical event sequences** (same number
  of A_LINE_DEL, B_LINE_ADD, A_LINE_CHANGE, B_LINE_CHANGE, A_GAP, B_GAP,
  A_SYMBOL_DEL, B_SYMBOL_ADD, A_DECOR_*, B_DECOR_* and ALIGN events)
  for typical inputs.
- The diffmap (used by jump-next/prev) is identical.
- The unified-diff path (`Differ.unidiff`) works with both
  `autojunk=True` and `autojunk=False`.
- No exceptions in any code path.

`scripts/test_stress.py` additionally verifies:

- All `.py` files in the plugin pass Python 3.8 syntax check
  (via `ast.parse(..., feature_version=(3, 8))`).
- `MyersSequenceMatcher` can be reused via `set_seq1`/`set_seq2`/`set_seqs`
  without raising `AttributeError` (the bug the `super().__init__()` fix
  addresses).
- Large-file diff (5000 lines) completes in <10ms with Myers (vs 17ms
  patience, 224ms difflib).

## How to merge future upstream myers.py updates

When you pull a new version of `myers.py` from Meld:

1. Replace `myers.py` with the new version.
2. Re-add the single `super().__init__(isjunk=None, a="", b="",
   autojunk=False)` call at the top of `MyersSequenceMatcher.__init__`.
   (If the upstream has added its own `super().__init__()` call in the
   meantime, you can skip this step.)
3. Run `python3 scripts/test_integration.py` to verify nothing broke.

That's it. No other files need to change.
