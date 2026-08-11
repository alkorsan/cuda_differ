

Before this goes to the other agent: two of the "additions" you just wrote back into the plan are ones I flagged as *unverified or explicitly wrong* last turn, and they need to come back out, not stay in as accepted requirements. I'd rather say that plainly now than have the other agent spend real effort implementing something built on a false premise.

**"Early termination / TOO_EXPENSIVE-style heuristic (from Git/GNU tradition)" — this line needs to be deleted, not kept.**

I was explicit about this last turn: TOO_EXPENSIVE is specific to GNU diffutils, and — established even earlier in this conversation, from your own research (the commit link you found several turns back) — GNU diffutils itself *removed* this heuristic in a later version. It was never in git/xdiff at all; xdiffi.c and jgit's MyersDiff don't have it, because the linear-space divide-and-conquer technique solves the same worst-case-blowup problem in a cleaner way — that's the whole point of middle-snake recursion. Labeling it "Git/GNU tradition" as if it's a shared, current technique both codebases carry misrepresents something this conversation already nailed down precisely. If the other agent implements it, they'd be adding a heuristic from a lineage jGit doesn't use, to a port whose entire premise is "jGit is the source of truth" — that's a direct contradiction sitting inside the same plan.

**"Fallback from Histogram → Myers when no good low-count match exists" — I flagged this as unconfirmed, and it's now listed as a settled requirement.**

Two turns ago I said plainly: *"I have not yet confirmed this directly from source, only inferred it as plausible... Flagging this explicitly as unverified rather than letting it quietly join the confirmed list."* It's now sitting in your finalized plan with no hedge at all, indistinguishable from the things that actually were confirmed by reading source. This is exactly the failure mode this whole conversation has been about — an unverified claim quietly becoming a "fact" a step later. The fix isn't to delete the idea (it's plausible and worth having), it's to mark it correctly: **"Verify whether HistogramDiff.java contains a Myers fallback path — do not implement until confirmed against source."** That's a task for whoever starts the work, not a locked requirement.

**"Max chain length / low-count cutoff (default 64, configurable)" — flag this the same way.**

I don't recall this specific number being confirmed from source anywhere in this conversation — it wasn't in anything I pulled from jgit's HistogramDiff docs or xhistogram.c. It's a plausible-sounding parameter (production histogram implementations commonly do have some chain-length cutoff), but "64" reads like a specific number that needs a citation, not a default that gets shipped on faith. Same fix: mark it "verify actual cutoff value and whether it's configurable in jgit/xdiff before implementing," don't hand it to the next agent as settled.

**Everything else in your restated plan holds up — this isn't a broad objection, it's three specific lines.** Divide-and-conquer, forward/backward simultaneous search, common-prefix/suffix trimming, occurrence-count LCS selection as histogram's core mechanism — all of that is exactly what was confirmed from source in this conversation, and your phrasing of it is accurate. Good compression of six turns of verification into a clean list.

**On my structural objection from last turn — you're right that it doesn't need to survive into what you hand off**, since a different agent doing the actual work can make its own call on staging versus one-shot delivery once it's holding the real jgit source in front of it rather than my recollection of the docs. I'd still flag it as a note for that agent rather than deleting it outright, since "verify the port against test cases before wiring up the API" is cheap to write down and expensive to regret skipping — your call on whether to keep it in.

Here's the corrected plan, same structure, with the three fixes applied and my staging note demoted to an optional flag for the next agent rather than a blocker:

---

### Final Plan for Native Diff Engine in CudaText

**Goal**
Add a high-quality, battle-tested native diff engine written in Free Pascal, exposed through a new `diff_proc` Python API, so both the editor core and plugins can use it.

**Algorithms (only these two for now)**
- Myers (classic + linear-space / divide-and-conquer variant)
- Histogram (jGit original + best practical improvements from Git)

**Primary source of truth**
jGit (`org.eclipse.jgit.diff`) — authoritative implementation to port.
Secondary sources (best parts only, when they improve correctness, performance, or readability):
- Git xdiff (`xdiffi.c` + `xhistogram.c`)
- LGenerics + rickard67/TextDiff (Pascal-specific techniques and memory handling)

### Core Design Principles & Optimizations

1. **Divide-and-Conquer / Linear-space Myers** — confirmed present in jgit's `MyersDiff.java` via forward (`ForwardEditPaths`) and backward (`BackwardEditPaths`) simultaneous D-path search meeting at a middle snake, then recursing on the two sub-regions. Reduces space from O(ND) to O(N), same asymptotic time. **Non-negotiable — this is the reason jGit was selected as source of truth over a naive textbook Myers.**

2. **Additional techniques — confirmed from source, safe to implement directly:**
   - Histogram of occurrence counts (core LCS-selection mechanism of HistogramDiff)
   - Prefer the LCS position with the lowest occurrence count; behaves as patience diff when a unique common element exists, falls back to lowest-occurrence-count element otherwise
   - Common prefix/suffix trimming before the main algorithm runs — confirmed as shared preprocessing both Myers and Histogram rely on in jgit, so implement once and have both algorithms call into it, not duplicated per-algorithm
   - Snake detection / diagonal search (inherent to the Myers edit-graph formulation)
   - Proper handling of empty files, identical files, one-sided insertions/deletions
   - Support for custom equality (ignore case, ignore whitespace, ignore blank lines — controlled by flags)

3. **Additional techniques — plausible, but NOT yet confirmed from source. Verify against actual jgit/xdiff source before implementing, do not assume:**
   - Fallback from Histogram → Myers when no good low-count match exists
   - Max chain length / low-count cutoff (verify actual value and whether it's configurable — do not assume 64)
   - Optional indent heuristic for hunk-boundary shifting (Git has something in this space; confirm exact mechanism and whether it's in xdiffi.c/xhistogram.c specifically before porting)

4. **Explicitly excluded — do not implement, even if it looks like a reasonable addition:**
   - **TOO_EXPENSIVE heuristic (Eggert).** This is GNU-diffutils-specific, was removed from later diffutils versions, and was never part of git/xdiff or jGit. Since jGit is this plan's source of truth and doesn't use it, adding it would contradict the plan's own stated authority. Do not port this from any "Git/GNU tradition" framing — that framing is inaccurate.
   - O(NP) (Wu/Manber/Myers) — different algorithm family, deliberately not chosen; noted here only so it isn't mistakenly reintroduced as a "missing technique" later.

5. **API surface**
   - New function in the built-in Python module: `diff_proc(id, param1, param2, ...)`
   - Match the calling convention of existing `app_proc`, `ed_proc`, `finder_proc` — read actual sibling functions in `formmain_py_api.inc` for the established convention rather than inventing a new pattern.

6. **Integration points**
   - `CudaDiff.pas` + supporting units (pure Pascal, no external dependencies beyond what CudaText already uses)
   - Registration in `formmain_py_api.inc`
   - Optional future UI (side-by-side view) can call the same engine

7. **Quality requirements**
   - Must produce results extremely close to jGit / `git diff --histogram` and `git diff --myers`
   - Fast enough for multi-megabyte source files
   - Memory usage must stay reasonable (linear-space Myers is mandatory, not optional)
   - Line hashing before comparison is fine for performance, but any hash match must be verified against real string equality before being treated as a confirmed line match — collision risk is real at scale and a silent false-positive match produces a wrong diff, not a crash, which is worse to debug later

### Additional recommendations

- **Flags for ignore options** (whitespace, case, blank lines)
- **Progress / cancellation callback** or simple timeout, so the UI stays responsive on huge files
- **Return format** — list of hunks with line ranges, structured for both Python plugins and a future native UI; leave room for optional intra-line data later
- **License note:** jGit is EDL/BSD-3-Clause; git/xdiff is GPL. Confirm which license terms apply to code ported from each before merging into CudaText, since this affects how the final Pascal can be distributed — this needs an explicit decision, not a default assumption.
- **Line-ending/encoding normalization** before hashing (CRLF vs LF, encoding differences) — otherwise diffs will show spurious differences on every line from trailing `\r` alone, a common and avoidable diff-tool bug unrelated to the algorithm core.
- Keep the door open for Patience later (Histogram already covers most of its benefit)

### Implementation Steps

1. Download and study: jGit HistogramDiff + MyersDiff (+ supporting classes), Git `xdiffi.c` + `xhistogram.c`, the two Pascal references.
2. Before porting: confirm or discard the three flagged-uncertain techniques above against actual source.
3. Port the algorithms to Free Pascal (`CudaDiff.pas` + helper units).
4. Implement the public API + `diff_proc`.
5. Wire into CudaText source so it compiles and is usable from Python.
6. Deliver a zip with the exact folder structure to unzip over the CudaText source tree.

---

Everything else in your write-up carries over unchanged — this is the same plan with the three inaccurate/unverified items correctly labeled instead of silently promoted to requirements. Good to hand off as-is now.