#!/usr/bin/env python3
"""
# create tests for native histogram optimization 1 opt4 to see the effect of OPT4_ENABLE_MYERS_D_CAP
autogen_test_8_blocks__Myers_Histogram_Poison .txt
- myers native took 6min
- histogram
    OPT4_ENABLE_MYERS_D_CAP = False     5min
    OPT4_ENABLE_MYERS_D_CAP = True      1.33m
	
autogen_test_7_sprinkle__Histogram_Poison
- myers native took 8s
- histogram
    OPT4_ENABLE_MYERS_D_CAP = False     22m
    OPT4_ENABLE_MYERS_D_CAP = True      1m 16s
"""


"""Generator for OPT-4 demonstration files.

Produces file pairs that make TMyersMiddleEdit.Calculate's MYERS_MAX_D
cap actually bind (edit distance of the region handed to Myers is
greater than ~2 x 16384 = 32768), so that toggling
OPT4_ENABLE_MYERS_D_CAP in cudadiffhistogram3.pas produces a clearly
visible seconds-vs-minutes difference.

Modes
-----
alldiff   A and B share ZERO lines (all lines unique, distinct
          prefixes). Use with AAlgo = 0 (cAlgoMyers): the whole file is
          one Myers region with D = N+M. NOTE: through the histogram
          engine (AAlgo = 1) this pair is instant in BOTH switch
          positions (no common element -> no punt -> histogram emits one
          replace itself) - that is expected, not a failure.

sprinkle  Every 2nd line is one of 4 boilerplate lines (occurring
          N/8 times each, far above maxChainLength=64), the rest are
          unique per side. Common elements exist but none is rare
          enough, so find_lcs punts the ROOT region to Myers with
          D ~= N. Use with AAlgo = 1 (histogram): the realistic
          worst case (two generated reports sharing only template
          lines). Also works with AAlgo = 0.

blocks    A = "A B C D" blocks repeated; B = same blocks, each
          independently shuffled. Chains >> 64 -> root punt, D ~= 2-4
          per block. Use with AAlgo = 1 (histogram).

All modes are seeded (default seed 20260901) so files regenerate
identically.

Usage
-----
  python3 gen_opt4_demo_files.py                 # writes all 3 pairs at
                                                 # default sizes below
  python3 gen_opt4_demo_files.py --mode sprinkle --lines 60000
  python3 gen_opt4_demo_files.py --verify-only

What to expect (FPC -O2, your build flags may vary):
  OPT4_ENABLE_MYERS_D_CAP = False : the Myers middle-snake search runs
      to its natural meeting depth (~N/2 d-iterations, total work
      O((N+M) x D)) -> tens of seconds to several minutes.
  OPT4_ENABLE_MYERS_D_CAP = True  : the search gives up at d = 16384
      and the region is emitted as ONE replace edit -> a few seconds.
      The diff view then shows a single giant hunk covering the whole
      region - that is the documented controlled divergence of OPT-4,
      not a bug.
"""
import argparse
import random
import os
import sys

SEED = 20260901
DEFAULTS = {
    "autogen_test_6_alldiff": 100000,
    "autogen_test_7_sprinkle__Histogram_Poison": 100000,
    "autogen_test_8_blocks__Myers_Histogram_Poison": 20000,  # blocks; x4 lines each
}
# PREFIX = "opt4_"
PREFIX = ""


def gen_alldiff(n, rng):
    la = [f"L{i:07d} {rng.random():.17f}" for i in range(n)]
    lb = [f"R{i:07d} {rng.random():.17f}" for i in range(n)]
    return la, lb


def gen_sprinkle(n, rng):
    unit = ["A", "B", "C", "D"]
    la, lb = [], []
    for i in range(n):
        if i % 2 == 0:
            s = unit[(i // 2) % 4]
            la.append(s)
            lb.append(s)
        else:
            la.append(f"x{i:07d} {rng.random():.17f}")
            lb.append(f"y{i:07d} {rng.random():.17f}")
    return la, lb


def gen_blocks(nblocks, rng):
    unit = ["A", "B", "C", "D"]
    la, lb = [], []
    for _ in range(nblocks):
        la.extend(unit)
        p = unit[:]
        rng.shuffle(p)
        lb.extend(p)
    return la, lb


def write_lines(path, lines):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")


def read_lines(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read().splitlines()


def generate(mode, n, out_dir, rng):
    if mode == "autogen_test_8_blocks__Myers_Histogram_Poison":
        la, lb = gen_blocks(n, rng)
    else:
        la, lb = (gen_alldiff if mode == "autogen_test_6_alldiff" else gen_sprinkle)(n, rng)
    pa = os.path.join(out_dir, f"{PREFIX}{mode}_a.txt")
    pb = os.path.join(out_dir, f"{PREFIX}{mode}_b.txt")
    write_lines(pa, la)
    write_lines(pb, lb)
    print("wrote %s (%d lines) and %s (%d lines)" % (pa, len(la), pb, len(lb)))
    return pa, pb


def verify(mode, pa, pb):
    a = read_lines(pa)
    b = read_lines(pb)
    sa, sb = set(a), set(b)
    ok = True
    common = sa & sb
    if mode == "autogen_test_6_alldiff":
        ok &= (len(sa) == len(a)) and (len(sb) == len(b))
        ok &= (len(common) == 0)
        expect = "0 common lines, all unique within each side"
    elif mode == "autogen_test_7_sprinkle__Histogram_Poison":
        letters = {"A", "B", "C", "D"}
        ok &= (common == letters)
        cnt = {c: a.count(c) for c in letters}
        ok &= all(v == len(a) // 8 for v in cnt.values())
        ok &= all(a[i] == b[i] for i in range(0, len(a), 2))
        expect = ("only the 4 boilerplate lines in common, %d occurrences each"
                  % (len(a) // 8))
    else:  # blocks
        ok &= (sa == {"A", "B", "C", "D"}) and (sb == sa)
        ok &= (len(a) % 4 == 0)
        diff_blocks = sum(
            1 for i in range(0, len(a), 4) if a[i:i + 4] != b[i:i + 4])
        expect = ("4 distinct lines each side; %d/%d blocks differ"
                  % (diff_blocks, len(a) // 4))
    print("  verify[%s]: %s -> %s" % (mode, "OK" if ok else "FAIL", expect))
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=list(DEFAULTS) + ["all"], default="all")
    ap.add_argument("--lines", type=int, default=None,
                    help="lines per side (blocks mode: number of blocks)")
    ap.add_argument("--out-dir", default=os.path.dirname(os.path.abspath(__file__)))
    ap.add_argument("--seed", type=int, default=SEED)
    ap.add_argument("--verify-only", action="store_true")
    args = ap.parse_args()

    os.makedirs(args.out_dir, exist_ok=True)
    modes = list(DEFAULTS) if args.mode == "all" else [args.mode]
    all_ok = True
    for m in modes:
        n = args.lines if args.lines is not None else DEFAULTS[m]
        pa = os.path.join(args.out_dir, f"{PREFIX}{m}_a.txt")
        pb = os.path.join(args.out_dir, f"{PREFIX}{m}_b.txt")
        if not args.verify_only:
            generate(m, n, os.path.join(args.out_dir), random.Random(args.seed))
        if os.path.exists(pa) and os.path.exists(pb):
            all_ok &= verify(m, pa, pb)
        else:
            print("  verify[%s]: SKIP (files missing)" % m)
    print("RESULT: %s" % ("all OK" if all_ok else "PROBLEMS FOUND"))
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
