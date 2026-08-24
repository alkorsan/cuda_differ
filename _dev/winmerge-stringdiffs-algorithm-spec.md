# WinMerge `stringdiffs.cpp` — full algorithm extraction for porting

Source: `WinMerge/winmerge`, commit `a7a99d71498dc92a22cb85d62576e6147fe2eb11`
File: `Src/stringdiffs.cpp` (1115 lines) + the two headers it depends on:
`Src/stringdiffs.h` (public API) and `Src/stringdiffsi.h` (private class decl).
Permalink: https://github.com/WinMerge/winmerge/blob/a7a99d71498dc92a22cb85d62576e6147fe2eb11/Src/stringdiffs.cpp

**Scope.** This covers everything in `stringdiffs.cpp` line-for-line. The 2-file
("string diff") pipeline is given in full, mechanical detail — that's the actual
algorithm. The `nFiles==3` dispatch branch also lives in this file (it's glue
code), so it's covered too, but at a lighter level, because the 3-way merge
itself (`Make3wayDiff`) is defined in `Diff3.h/.cpp`, which is **not** in this
file and not fetched here — say the word if you want that traced too.

Nothing below is simplified away silently. Where the original C++ has a quirk,
an asymmetry, or dead code, it's called out explicitly rather than smoothed
over — see the **Gotchas** section at the end for the consolidated list.

---

## 0. External dependencies you must replace

| Original uses | Purpose | Port with |
|---|---|---|
| `ICUBreakIterator` (ICU `UBRK_CHARACTER`) | walk the string one *extended grapheme cluster* at a time (not one UTF-16 code unit) | your Unicode lib's grapheme-cluster segmenter (ICU bindings, Rust `unicode-segmentation`, JS `Intl.Segmenter`, Python `regex` grapheme mode, …) — or consciously downgrade to codepoint iteration, see Gotcha #7 |
| `GetStringTypeW(CT_CTYPE1, …)` | classify a "wide" (≥ U+0100) character as upper/lower/digit for word-breaking | a Unicode general-category lookup: treat `Lu`/`Ll`/`Lt` (has case) or `Nd` (decimal digit) as "keep together"; anything else (`Lo`, `P*`, `S*`, …) is a break char — see §3 |
| `IsDBCSLeadByte` / `_getmbcp` | legacy ANSI/DBCS lead-byte detection | drop entirely — only compiled in non-Unicode builds; under `UNICODE` (WinMerge's normal build) it's always `false` |
| `std::chrono::system_clock` | 500 ms wall-clock budget for the O(NP) engine | any monotonic clock |

Everything else (the actual diff logic) is portable arithmetic/string-indexing.

---

## 1. Data model & constants

```
enum EolCompareMode   { EOL_STRICT = 0, EOL_IGNORE = 1, EOL_AS_SPACE = 2 }
enum WhitespaceMode    { WHITESPACE_COMPARE_ALL = 0, WHITESPACE_IGNORE_CHANGE = 1, WHITESPACE_IGNORE_ALL = 2 }
enum WordBreakClass    { WORD = 0, SPACE = 1, EOL = 2, BREAK = 3, NUMBER = 4 }   // original names: dlword, dlspace, dleol, dlbreak, dlnumber
```

`breakType` (an int *parameter*, not the enum above) controls punctuation
splitting: `0` = break words on whitespace only; `1` = also break on the
configured punctuation set. Default punctuation set = `",.;:"` (comma, period,
semicolon, colon), overridable via `SetBreakChars()`. This is process-global
mutable state in the original (`static tchar_t *BreakChars`) — port it as
whatever config-scoping makes sense for you (global, per-instance, whatever).

```
struct Word {
    start:  int      // index of first code unit of this token, in the ORIGINAL string
    end:    int      // index of last code unit (inclusive)
    hash:   uint32   // rolling hash, see §4 (0 for the dummy sentinel)
    kind:   WordBreakClass
    length: () -> int { return end - start + 1 }
}

struct WordDiff {
    begin: [int, int, int]   // begin[0] in str1, begin[1] in str2, begin[2] only used in 3-way
    end:   [int, int, int]   // inclusive; end < begin  <=>  "empty/anchor" range on that side
}
```

**Convention used throughout:** a side with `begin == end + 1` (i.e. `end = begin
- 1`) means *no content on that side* — it's an anchor position for a pure
insertion or pure deletion, not a real range. `begin == -1` is the sentinel
some functions use to mean "no diff at all on this side." Keep these two
distinct conventions straight — they're not interchangeable, and the code
switches between them depending on which function you're in.

Tunables (safety valves, not algorithmic requirements — pick your own numbers
if you like):
- **Word-count cap** before falling back to a trivial "whole string differs"
  result: `20480` (64-bit build) / `2048` (32-bit build) — purely a memory/perf
  circuit breaker on the O(NP) arrays, which are O(M+N).
- **Time budget** for the O(NP) engine: `500 ms`, checked every `100000`
  snake-computations.

Two struct fields in the original are declared but **dead** at this revision —
don't spend time hunting for their purpose, they don't have one here:
- `m_matchblock` (constructor comment: *"Change to false to get word to word
  compare"*) — set to `true` and never read anywhere else in the file.
- `dp()` — declared in the header, only referenced from a commented-out call
  site (`//if (dp(edscript) <= 0)`); `onp()` is the live algorithm.

---

## 2. Pipeline overview (2-file case)

```
ComputeWordDiffs(str1, str2, opts, byteLevel)
  1. words1 = Tokenize(str1)                     §3
     words2 = Tokenize(str2)
  2. if word counts under the size cap:
         wdiffs = WordLevelDiff(words1, words2)   §5–§7  (O(NP) LCS + edit-script walk)
     else / on timeout:
         wdiffs = [ one WordDiff spanning the whole of both strings ]   (fallback)
  3. if byteLevel:
         for each wdiff: shrink it to the minimal differing character range   §8–§9
  4. merge any now-adjacent wdiffs, emit final diff list                      §10
```

Everything downstream keys off **word tokens**, not characters — the O(NP)
engine's "equal / not equal" atoms are tokens (§6's `AreWordsSame`), and the
optional byte-level pass (§8) only *narrows* each already-found word-level
diff down to the exact character span; it never searches outside a word-level
diff's boundaries.

---

## 3. Tokenizer — `BuildWordsArray(str)`

Breaks a string into `Word` tokens. Building block for everything else.

```
function tokenize(str, opts) -> Word[]:
    words = [ Word(start=0, end=-1, hash=0, kind=WORD) ]   // index 0: dummy sentinel
    if str.isEmpty(): return words

    i = 0
    begin = 0
    kind = WORD
    prevKind = WORD

    while i < len(str):
        kind = WORD
        ch = str[i]

        if ch == '\r' or ch == '\n':
            kind = (opts.eolMode == EOL_AS_SPACE) ? SPACE : EOL
        elif isSafeWhitespace(ch):                                   // §3a
            kind = SPACE
        elif isWordBreak(opts.breakType, str, i):                    // §3b
            kind = BREAK
        elif opts.ignoreNumbers and isDigit(ch):
            kind = NUMBER
        // else: stays WORD

        // close out the run that's ENDING and start a new one, whenever:
        //   - the classification changed, OR
        //   - we just landed on a BREAK char (every break/punct char is its
        //     own single-char token, even if the previous char was ALSO break), OR
        //   - the previous char was EOL-kind and this pair isn't "\r\n"
        //     (keeps a \r\n pair together as one token, but not a lone \r or \n)
        if i > 0 and (kind != prevKind
                      or kind == BREAK
                      or (prevKind == EOL and not (str[i-1] == '\r' and ch == '\n'))):
            words.push(Word(begin, i - 1, hash(str, begin, i - 1), prevKind))
            begin = i

        if opts.eolMode == EOL_AS_SPACE and kind == SPACE:
            // coalesce a whole run of CR / LF / whitespace into ONE token
            while i < len(str):
                ch2 = str[i]
                if ch2 != '\r' and ch2 != '\n' and not isSafeWhitespace(ch2):
                    break
                i += 1
            // (if your segmenter is stateful/cursor-based like ICU's, re-sync
            // its cursor to raw index i here; a stateless nextChar(str, idx)
            // needs no such step — see Gotcha #6)
        else:
            i = nextChar(str, i)     // advance ONE grapheme cluster

        prevKind = kind

    // flush the final trailing run — note this uses `kind` (the classification
    // of the LAST character processed), not `prevKind`
    words.push(Word(begin, i - 1, hash(str, begin, i - 1), kind))
    return words
```

Net effect: one token per maximal run of same-classified characters, **except**
every `BREAK` (punctuation) character always gets its own single-character
token, and (only under `EOL_AS_SPACE`) runs of whitespace/CR/LF get merged into
one `SPACE` token.

### 3a. `isSafeWhitespace(ch)`

```
function isSafeWhitespace(ch) -> bool:
    return isUnicodeWhitespace(ch) and not isLeadByte(ch) and ch != '\r' and ch != '\n'
```
`\r`/`\n` are deliberately excluded — they're handled as `EOL`, not generic
whitespace. `isLeadByte` is the legacy-DBCS check from §0 — always `false` on a
clean Unicode port, drop it.

### 3b. `isWordBreak(breakType, str, index)`

```
function isWordBreak(breakType, str, index) -> bool:
    ch = str[index]
    if codepoint(ch) < 0x100:                       // Latin-1 range
        if breakType == 0: return false              // whitespace-only mode: nothing breaks
        return ch in BreakChars                       // default set ",.;:"
    else:
        // NOTE: breakType is NOT consulted in this branch — see Gotcha #1
        cls = unicodeCharacterClass(ch)               // Win32 CT_CTYPE1 equivalent
        return not (cls has_case or cls is_digit)     // "has_case" = would be flagged upper OR lower
```
The wide-character branch is a Win32 `GetStringTypeW(CT_CTYPE1, …)` call,
testing the returned flags against `C1_UPPER | C1_LOWER | C1_DIGIT` — true
(break) iff **none** of those are set. Practical effect: Cyrillic/Greek/etc.
letters (which have case) are *not* break chars and group normally; CJK
ideographs (`Lo`, no case) *are* break chars, so CJK text ends up tokenized
essentially one character per token — an intentional stand-in for "words" in
scripts without inter-word spaces. Nearest portable equivalent: general
category `Lu`/`Ll`/`Lt` (has case) or `Nd` (digit) → not a break char;
anything else, including `Lo`, `P*`, `S*`, `N*` (non-decimal) → break char.

---

## 4. Hash — `Hash(str, begin, end, h=0)`

diffutils-style rolling hash, 32-bit unsigned, **must wrap on overflow** —
use `uint32` (or explicit `& 0xFFFFFFFF` masking) in your port:

```
ROL(v, n)   = (v << n) | (v >> (32 - n))        // 32-bit left-rotate; assumes `unsigned` = 32 bits
HASH(h, c)  = c + ROL(h, 7)

function hash(str, begin, end) -> uint32:
    h = 0
    for i in begin..end:
        ch = codeUnit(str[i]) as uint32
        if not caseSensitive: ch = toLower(ch)
        h = h + HASH(h, ch)              // i.e. h = h + ch + ROL(h, 7), wrapping mod 2^32
    return h
```

Used only as a cheap pre-filter in §6 (`AreWordsSame`) before the real
character comparison — collisions just cost you a redundant char-compare,
they don't affect correctness.

---

## 5. Word equality oracle — `AreWordsSame(word1, word2)`

This is **not** a plain string comparison. It's the equality predicate fed
into the O(NP) engine, and it has several unconditional short-circuits, checked
**in this order**:

```
function areWordsSame(word1, word2, str1, str2, opts) -> bool:

    // 1. whitespace-mode short-circuit
    if opts.whitespace != WHITESPACE_COMPARE_ALL:
        if word1.kind == SPACE and word2.kind == SPACE:
            return true    // ANY whitespace token equals ANY other whitespace
                            // token — length and content are not compared

    // 2. ignore-numbers short-circuit — loose, see Gotcha #2
    if opts.ignoreNumbers:
        if isDigit(str1[word1.start]) and isDigit(str2[word2.start]):
            return true    // true if BOTH tokens merely *start* with a digit —
                            // not gated on word.kind == NUMBER, not comparing value/length

    // 3. EOL-ignore short-circuit
    if opts.eolMode == EOL_IGNORE:
        if word1.kind == EOL and word2.kind == EOL:
            return true    // any EOL token equals any other (CR, LF, CRLF all equal)

    // 4. EOL-as-space: structural whitespace compare
    elif opts.eolMode == EOL_AS_SPACE:
        if word1.kind == SPACE and word2.kind == SPACE:
            s1 = normalizeCrLfToSpace(str1.substr(word1.start, word1.length()))
            s2 = normalizeCrLfToSpace(str2.substr(word2.start, word2.length()))
            return s1 == s2     // exact compare AFTER normalizing \r\n and lone \r/\n to ' '

    // 5. fallback: real content comparison
    if word1.hash != word2.hash: return false
    if word1.length() != word2.length(): return false
    for i in 0..word1.length()-1:
        c1 = str1[word1.start + i]
        c2 = str2[word2.start + i]
        if opts.caseSensitive: if c1 != c2: return false
        else:                  if toLower(c1) != toLower(c2): return false
    return true
```

`normalizeCrLfToSpace` = the same two-pass replace used elsewhere: first
replace the literal 2-char sequence `"\r\n"` with `" "`, **then** replace any
remaining lone `\r` or `\n` with `" "` each (see §11's note on `replace` vs
`replace_chars` — same pattern here).

---

## 6. O(NP) sequence comparison — `onp()` + `snake()`

Wu/Manber/Myers/Chen's O(NP) diff algorithm, computing an edit script over the
two token arrays (index 0 = dummy sentinel in both, so "real" tokens are
`words[1..]`). This is transcribed **mechanically** below — the *why* of each
branch is the O(NP) paper's territory; what matters for a faithful port is
reproducing these exact operations in this exact order.

```
function onp(words1, words2, opts) -> char[] | TIMEOUT:
    M = len(words1) - 1        // real word count, sequence "A"
    N = len(words2) - 1        // real word count, sequence "B"
    exchanged = (M > N)
    if exchanged: swap(M, N)   // ensure M <= N (the O(NP) bound wants the shorter seq as M)

    // fp indexed from -(M+1) .. (N+1) inclusive — needs negative-index support;
    // offset by (M+1) if your language doesn't have it natively
    fp = array[-(M+1) .. (N+1)] of int, all initialized to -1

    // es[k]: growable list of {op, neq, pk, pi} per diagonal k, same index range as fp.
    // fp[] and es[] are BOTH mutated in place across the whole run — never reset per round.
    es = array[-(M+1) .. (N+1)] of empty list

    DELTA = N - M

    function addEditScriptElem(k):
        if fp[k-1] + 1 > fp[k+1]:
            op = '+'; neq = fp[k] - (fp[k-1] + 1); pk = k - 1
        else:
            op = '-'; neq = fp[k] - fp[k+1];        pk = k + 1
        pi = len(es[pk]) - 1     // back-pointer: most-recently-pushed elem on the parent diagonal
        es[k].push({op, neq, pk, pi})

    count = 0
    startTime = now()
    p = -1
    repeat:
        p += 1
        for k = -p to DELTA-1 step +1:                       // ascending sweep
            fp[k] = snake(k, max(fp[k-1]+1, fp[k+1]), M, N, exchanged, words1, words2)
            addEditScriptElem(k); count += 1
        for k = DELTA+p downto DELTA+1 step -1:               // descending sweep
            fp[k] = snake(k, max(fp[k-1]+1, fp[k+1]), M, N, exchanged, words1, words2)
            addEditScriptElem(k); count += 1
        k = DELTA                                              // middle diagonal, always last
        fp[k] = snake(k, max(fp[k-1]+1, fp[k+1]), M, N, exchanged, words1, words2)
        addEditScriptElem(k); count += 1

        if count > 100000:
            count = 0
            if (now() - startTime) > 500ms:
                free fp, es
                return TIMEOUT       // caller must fall back — see §2 step 2 / §7
    until fp[k] == N          // k == DELTA here, from the block just above

    // ---- reconstruct the raw op sequence by walking back-pointers ----
    ses = []
    k = DELTA
    i = len(es[DELTA]) - 1
    while i >= 0:
        e = es[k][i]
        repeat e.neq times: ses.push('=')
        ses.push(e.op)
        i = e.pi
        k = e.pk
    reverse(ses)

    // ---- collapse adjacent opposite +/- pairs into '!' (substitution); undo the M/N swap ----
    editScript = []
    n = 1                      // NOTE: ses[0] is skipped deliberately, start at n=1
    cnt = len(ses)
    while n < cnt:
        ch = ses[n]
        if ch == '+' or ch == '-':
            isPlus = (ch == '+')
            if n != cnt - 1 and ses[n+1] == (isPlus ? '-' : '+'):
                n += 1                          // consume the paired opposite op too
                c = '!'
            else:
                c = (exchanged == isPlus) ? '-' : '+'   // un-swap for the M/N exchange
            // (D counter in the original increments here; its magnitude is
            // discarded by the only caller in this file — sign/TIMEOUT is all that matters)
        else:
            c = '='
        editScript.push(c)
        n += 1

    free fp, es
    return editScript


function snake(k, y, M, N, exchanged, words1, words2) -> int:
    x = y - k
    if exchanged:
        while x < M and y < N and areWordsSame(words1[y+1], words2[x+1]):
            x += 1; y += 1
    else:
        while x < M and y < N and areWordsSame(words1[x+1], words2[y+1]):
            x += 1; y += 1
    return y
```

Watch the `n += 1` bookkeeping in the collapse loop closely if you transliterate
into a `for`-style loop in your target language: the pairing branch advances
`n` **once inside the `if`, plus once more at the bottom of the loop body** —
net +2, skipping both members of the pair. Get this wrong and every
substitution shifts the rest of the script by one.

---

## 7. Edit script → word-level diffs — `BuildWordDiffList_DP()`

```
function buildWordDiffListDP(words1, words2, opts) -> (ok: bool, wdiffs: WordDiff[]):
    editScript = onp(words1, words2, opts)
    if editScript == TIMEOUT: return (false, [])

    wdiffs = []
    i = 1   // cursor into words1 (1-based — index 0 is the dummy)
    j = 1   // cursor into words2

    for op in editScript:
        if op == '-':                             // word i exists in str1, nothing in str2
            if opts.whitespace == WHITESPACE_IGNORE_ALL and words1[i].kind == SPACE:
                i += 1; continue                   // suppressed — no diff emitted
            if opts.ignoreNumbers and words1[i].kind == NUMBER:
                i += 1; continue
            s1, e1 = words1[i].start, words1[i].end
            s2 = words2[j-1].end + 1; e2 = s2 - 1    // empty anchor range in str2
            wdiffs.push(WordDiff(begin=[s1,s2], end=[e1,e2]))
            i += 1

        elif op == '+':                            // word j exists in str2, nothing in str1
            if opts.whitespace == WHITESPACE_IGNORE_ALL and words2[j].kind == SPACE:
                j += 1; continue
            if opts.ignoreNumbers and words2[j].kind == NUMBER:
                j += 1; continue
            s1 = words1[i-1].end + 1; e1 = s1 - 1    // empty anchor range in str1
            s2, e2 = words2[j].start, words2[j].end
            wdiffs.push(WordDiff(begin=[s1,s2], end=[e1,e2]))
            j += 1

        elif op == '!':                            // substitution, real content both sides
            if (opts.whitespace == WHITESPACE_IGNORE_CHANGE or opts.whitespace == WHITESPACE_IGNORE_ALL)
               and words1[i].kind == SPACE and words2[j].kind == SPACE:
                i += 1; j += 1; continue
            if opts.ignoreNumbers and words1[i].kind == NUMBER and words2[j].kind == NUMBER:
                i += 1; j += 1; continue
            s1, e1 = words1[i].start, words1[i].end
            s2, e2 = words2[j].start, words2[j].end
            wdiffs.push(WordDiff(begin=[s1,s2], end=[e1,e2]))
            i += 1; j += 1

        else:   // '=' — match
            i += 1; j += 1

    return (true, wdiffs)
```

(Original has an `#ifdef STRINGDIFF_LOGGING` debug dump here — `OutputDebugString`
of each diff's text/position. Pure logging, not algorithmic; skip unless you
want an equivalent trace facility.)

---

## 8. Orchestration + fallback — `BuildWordDiffList()`

```
function buildWordDiffList(str1, str2, opts) -> WordDiff[]:
    words1 = tokenize(str1, opts)
    words2 = tokenize(str2, opts)

    LIMIT = 20480   // or 2048 — pick one; see §1
    ok = false
    if len(words1) < LIMIT and len(words2) < LIMIT:
        (ok, wdiffs) = buildWordDiffListDP(words1, words2, opts)

    if not ok:
        // fallback: too large for the DP pass, OR onp() timed out —
        // treat the ENTIRE strings as one single diff, maximally conservative
        s1, e1 = words1[0].start, words1[last].end
        s2, e2 = words2[0].start, words2[last].end
        return [ WordDiff(begin=[s1,s2], end=[e1,e2]) ]

    return wdiffs
```

---

## 9. Character-level refinement — `ComputeByteDiff()`

The most intricate function in the file. Given two (sub)strings, finds the
minimal differing character range by scanning matching characters in from
both ends — but with whitespace-skipping layered in on both scans, and a
handful of edge cases. Below uses **index-based cursors** (not raw pointers)
and two stateless helpers:

- `nextChar(str, idx)` — index of the start of the character immediately after
  the one starting at `idx`.
- `prevChar(str, idx)` — index of the start of the character immediately
  before `idx`.

(The original drives these off a *stateful* ICU `BreakIterator` cursor, mixing
`.next()`/`.previous()` — "move from wherever the cursor currently is" — with
occasional `.preceding(offset)`/`.following(offset)` — "look up the boundary
relative to this explicit offset, ignoring cursor history." This split is
purely an artifact of ICU's C++ API; see Gotcha #6 for why a stateless
`nextChar`/`prevChar` pair is a safe, recommended simplification, not a
fidelity loss.)

```
function computeByteDiff(str1, str2, opts, equal) -> {begin: [int,int], end: [int,int]}:
    len1, len2 = length(str1), length(str2)

    // --- empty-string edge case ---
    if len1 == 0 or len2 == 0:
        if len1 == len2:
            return { begin: [-1,-1], end: [-1,-1] }       // both empty -> no diff
        else:
            return { begin: [0,0], end: [len1-1, len2-1] } // whole non-empty side is the diff

    py1, py2 = 0, 0                        // forward cursors
    pen1 = prevChar(str1, len1)            // start index of the LAST character in str1
    pen2 = prevChar(str2, len2)
    glyphlenz1 = len1 - pen1               // code-unit length of that last character
    glyphlenz2 = len2 - pen2

    // --- trim leading/trailing whitespace (only if not comparing everything) ---
    if opts.whitespace != WHITESPACE_COMPARE_ALL:
        while py1 < pen1 and isSafeWhitespace(str1[py1]): py1 = nextChar(str1, py1)
        while py2 < pen2 and isSafeWhitespace(str2[py2]): py2 = nextChar(str2, py2)

        // guard against mismatched/broken multi-unit sequences right at the end —
        // a legacy DBCS-safety concern; a clean Unicode string never trips this,
        // so a modern port can treat this guard as always-true and always trim
        if not ((pen1 < len1-1 or pen2 < len2-1) and str1[len1] != str2[len2]):
            while pen1 > py1 and isSafeWhitespace(str1[pen1]): pen1 = prevChar(str1, pen1)
            while pen2 > py2 and isSafeWhitespace(str2[pen2]): pen2 = prevChar(str2, pen2)

    // --- edge case: after trimming, a side is nothing but one whitespace char
    //     sitting exactly at pen -> the WHOLE original range is the diff
    //     (only when equal == false; equal=true is never invoked from within
    //     this file, see §10, but preserve the parameter for other callers) ---
    if not equal and ((py1 == pen1 and isSafeWhitespace(str1[pen1])) or
                       (py2 == pen2 and isSafeWhitespace(str2[pen2]))):
        return { begin: [0,0], end: [len1-1, len2-1] }

    // ================= FORWARD SCAN: find start of difference =================
    while true:
        if py1 > pen1 and py2 > pen2:
            return { begin: [-1,-1], end: [-1,-1] }        // fully equal throughout
        if py1 > pen1 or py2 > pen2:
            break                                            // one side exhausted first

        if opts.whitespace and py1 < pen1 and isSafeWhitespace(str1[py1]):
            if opts.whitespace == WHITESPACE_IGNORE_CHANGE and not isSafeWhitespace(str2[py2]):
                break                                         // real mismatch: ws vs non-ws
            py1 = advanceOverWhitespace(str1, py1, pen1)
            py2 = advanceOverWhitespace(str2, py2, pen2)
            continue

        if opts.whitespace and py2 < pen2 and isSafeWhitespace(str2[py2]):
            if opts.whitespace == WHITESPACE_IGNORE_CHANGE and not isSafeWhitespace(str1[py1]):
                break
            py1 = advanceOverWhitespace(str1, py1, pen1)
            py2 = advanceOverWhitespace(str2, py2, pen2)
            continue

        py1next = nextChar(str1, py1)
        py2next = nextChar(str2, py2)
        glyphleny1 = py1next - py1
        glyphleny2 = py2next - py2
        if glyphleny1 != glyphleny2 or not matchchar(str1, py1, str2, py2, glyphleny1, opts.caseSensitive):
            break
        py1, py2 = py1next, py2next

    begin = [py1, py2]

    // ================= BACKWARD SCAN: find end of difference =================
    pz1, pz2 = pen1, pen2
    while true:
        if pz1 < py1 and pz2 < py2:
            return { begin: [-1,-1], end: [-1,-1] }         // region collapsed to nothing
        if pz1 < py1 or pz2 < py2:
            break

        if opts.whitespace and pz1 > py1 and isSafeWhitespace(str1[pz1]):
            if opts.whitespace == WHITESPACE_IGNORE_CHANGE and not isSafeWhitespace(str2[pz2]):
                break
            while pz1 > py1 and isSafeWhitespace(str1[pz1]): pz1 = prevChar(str1, pz1)
            while pz2 > py2 and isSafeWhitespace(str2[pz2]): pz2 = prevChar(str2, pz2)
            continue

        // *** asymmetric with the branch above — faithful to source, see Gotcha #3 ***
        // does NOT check str1[pz1]'s whitespace status before breaking, and
        // retreats pz2 ONLY (pz1 is left untouched here)
        if opts.whitespace and pz2 > py2 and isSafeWhitespace(str2[pz2]):
            if opts.whitespace == WHITESPACE_IGNORE_CHANGE:
                break
            while pz2 > py2 and isSafeWhitespace(str2[pz2]): pz2 = prevChar(str2, pz2)
            continue

        if glyphlenz1 != glyphlenz2 or not matchchar(str1, pz1, str2, pz2, glyphlenz1, opts.caseSensitive):
            break
        pz1next, pz2next = pz1, pz2
        pz1 = (pz1 > 0) ? prevChar(str1, pz1) : pz1 - 1
        pz2 = (pz2 > 0) ? prevChar(str2, pz2) : pz2 - 1
        glyphlenz1 = pz1next - pz1
        glyphlenz2 = pz2next - pz2

    end = [pz1 + glyphlenz1 - 1, pz2 + glyphlenz2 - 1]

    if begin[0] == end[0] + 1 and begin[1] == end[1] + 1:
        begin[0] = -1                                        // empty result -> "no diff" sentinel

    return { begin, end }


function advanceOverWhitespace(str, cur, limit) -> int:
    // may end up at limit+1 (one past the last valid char) — note the `<=`
    while cur <= limit and isSafeWhitespace(str[cur]):
        cur = nextChar(str, cur)
    return cur

function matchchar(str1, idx1, str2, idx2, len, caseSensitive) -> bool:
    if caseSensitive:
        return str1[idx1 : idx1+len] == str2[idx2 : idx2+len]     // raw code-unit compare
    for k in 0..len-1:
        if toLower(str1[idx1+k]) != toLower(str2[idx2+k]): return false
    return true
```

There's a commented-out (dead, never compiled) block right after the backward
scan in the original that attempts special-case `\r\n` boundary snapping —
it's inert, skip it.

---

## 10. Word-diff → byte-diff driver — `wordLevelToByteLevel()`

Runs `computeByteDiff` on each word-level diff's substrings and narrows the
diff to the returned local range, translating local → global coordinates:

```
function wordLevelToByteLevel(str1, str2, wdiffs, opts):
    for diff in wdiffs:
        sub1 = str1.substring(diff.begin[0], diff.end[0])   // empty if end < begin
        sub2 = str2.substring(diff.begin[1], diff.end[1])
        r = computeByteDiff(sub1, sub2, opts, equal=false)    // equal is ALWAYS false from this call site

        if r.begin[0] == -1:
            diff.end[0] = diff.begin[0] - 1                  // collapse to empty
        else:
            diff.end[0]   = diff.begin[0] + r.end[0]
            diff.begin[0] = diff.begin[0] + r.begin[0]        // note: begin updated using the OLD begin

        if r.begin[1] == -1:
            diff.end[1] = diff.begin[1] - 1
        else:
            diff.end[1]   = diff.begin[1] + r.end[1]
            diff.begin[1] = diff.begin[1] + r.begin[1]
```

(Watch the order in the `else` branches — `end` is computed from the *old*
`begin` before `begin` itself gets reassigned. Compute both offsets before
mutating, or you'll double-apply the shift.)

---

## 11. Adjacent-diff coalescing — `PopulateDiffs()`

Runs **after** the byte-level pass. Merges two consecutive word-diffs into one
whenever they're contiguous on both sides (no gap = no matching text between
them — an artifact of word tokenization splitting what's really one
contiguous change into separate word-diffs):

```
function populateDiffs(wdiffs) -> WordDiff[]:
    output = []
    for i in 0 .. len(wdiffs)-1:
        skip = false
        if i+1 < len(wdiffs):
            if wdiffs[i].end[0]+1 == wdiffs[i+1].begin[0] and
               wdiffs[i].end[1]+1 == wdiffs[i+1].begin[1]:
                wdiffs[i+1].begin[0] = wdiffs[i].begin[0]     // extend the NEXT diff backward
                wdiffs[i+1].begin[1] = wdiffs[i].begin[1]
                skip = true                                    // absorb i into i+1, emit nothing now
        if not skip:
            assert(wdiffs[i].begin[0] >= 0 or wdiffs[i].begin[1] >= 0)   // never both-anchor
            output.push(wdiffs[i])
    return output
```

---

## 12. Top-level entry points

### 12a. Two-file core

```
function computeWordDiffs(str1, str2, opts, byteLevel) -> WordDiff[]:
    wdiffs = buildWordDiffList(str1, str2, opts)     // §8
    if byteLevel:
        wordLevelToByteLevel(str1, str2, wdiffs, opts)  // §10
    return populateDiffs(wdiffs)                      // §11
```

This is the whole "string diff" algorithm. Everything above composes into
this one function.

### 12b. `nFiles` dispatcher (summarized — glue code, not new algorithm)

The public `ComputeWordDiffs(nFiles, strs[], …)` overload handles 2-way and
3-way callers uniformly:

- **`nFiles == 2`**: exactly §12a.
- **`nFiles == 3`, one of the three strings empty**: run §12a on the other two,
  then remap the resulting diffs' `[begin,end]` triples to shift which of the
  3 slots holds which file's data, marking the empty file's slot as an
  anchor (`begin = 0, end = -1`) throughout. Three symmetric cases
  (`strs[0]` empty / `strs[1]` empty / `strs[2]` empty), each just a slot
  relabeling after the fact — mechanical, not interesting.
- **`nFiles == 3`, all non-empty**: run §12a **twice** — middle-vs-left and
  middle-vs-right — **both forced to `WHITESPACE_COMPARE_ALL` regardless of
  the caller's requested whitespace option** (this is hardcoded in the
  dispatcher, not a passthrough — see Gotcha #4) — then hand both diff lists
  to `Make3wayDiff(...)` along with a same-length-byte-range equality functor
  (`Comp02Functor`, compares `strs[0]` vs `strs[2]` over matching-length
  ranges to detect when "left" and "right" actually agree with each other
  despite both differing from "middle"). **`Make3wayDiff` itself lives in
  `Diff3.h/.cpp`, outside this file — not traced here.**

### 12c. Bonus: `Compare()` — a separate, simpler whole-string comparator

Not part of the word-diff pipeline at all — a standalone options-aware
string comparison (`< 0` / `0` / `> 0`, like `strcmp`), presumably used
elsewhere in WinMerge for a fast "are these two lines identical under the
current options" check before bothering with word-diffing.

```
function compare(str1, str2, opts) -> int:
    // fast path: nothing to normalize
    if opts.caseSensitive and opts.eolMode == EOL_STRICT
       and opts.whitespace == WHITESPACE_COMPARE_ALL and not opts.ignoreNumbers:
        return compareOrdinal(str2, str1)        // note: str2 vs str1, reversed

    s1, s2 = str1, str2
    if not opts.caseSensitive: s1, s2 = toLower(s1), toLower(s2)

    if opts.eolMode == EOL_IGNORE:
        // per-CHARACTER class replace: '\r' -> '\n' AND '\n' -> '\n', independently.
        // "\r\n" becomes "\n\n" (length-preserving) -- this does NOT collapse the pair.
        replaceEachCharIn(s1, set:['\r','\n'], with:'\n')
        replaceEachCharIn(s2, set:['\r','\n'], with:'\n')
    elif opts.eolMode == EOL_AS_SPACE:
        // two passes: first collapse the literal SEQUENCE "\r\n" -> " ",
        // THEN mop up any leftover lone \r or \n (chars, not sequence) -> " " each
        replaceSequence(s1, "\r\n", " "); replaceEachCharIn(s1, set:['\r','\n'], with:' ')
        replaceSequence(s2, "\r\n", " "); replaceEachCharIn(s2, set:['\r','\n'], with:' ')

    if opts.whitespace == WHITESPACE_IGNORE_CHANGE:
        // maps tab->space 1-for-1, does NOT collapse run lengths -- see Gotcha #5
        replaceEachCharIn(s1, set:[' ','\t'], with:' ')
        replaceEachCharIn(s2, set:[' ','\t'], with:' ')
    elif opts.whitespace == WHITESPACE_IGNORE_ALL:
        deleteEachCharIn(s1, set:[' ','\t'])
        deleteEachCharIn(s2, set:[' ','\t'])

    if opts.ignoreNumbers:
        deleteEachCharIn(s1, set:'0123456789')
        deleteEachCharIn(s2, set:'0123456789')

    return compareOrdinal(s2, s1)      // reversed order again
```

---

## 13. Config surface — `Init` / `Close` / `SetBreakChars`

Trivial global state management for the punctuation break-char set:

```
BreakChars = ",.;:"                       // Init(): reset to default
SetBreakChars(newChars):  BreakChars = newChars   // used by §3b's ASCII branch
Close():  free BreakChars if it was ever customized
```
Port as whatever config-lifetime story fits your target (module global,
constructor param, whatever) — no algorithmic content here.

---

## Gotchas — read before you port, not after

Numbered for cross-reference back to the sections above.

1. **§3b asymmetry, ASCII vs wide punctuation.** `breakType == 0` ("whitespace
   only, no punctuation breaking") is only honored in the **ASCII/Latin-1**
   branch (`codepoint < 0x100`). The wide-character branch ignores `breakType`
   completely — punctuation/symbol/CJK-ideograph splitting is **always
   active** for non-Latin-1 text, regardless of what the caller asked for.
   Preserve this if you want output-parity with WinMerge; "fix" it only if you
   deliberately want to diverge.

2. **§5 rule 2, the ignore-numbers short-circuit is loose.** It checks whether
   the token's *first character* is a digit on both sides — not the token's
   `NUMBER` classification, not its length, not its value. `"123"` and
   `"456789"` are "the same" under this rule; so are `"1"` and `"1x"` if `"1x"`
   somehow got classified starting with a digit. Faithful port = replicate the
   looseness; don't "improve" it into a real numeric-equality check unless you
   mean to.

3. **§9 backward-scan whitespace branches are not mirror images of each
   other.** The `pz1`-is-whitespace branch checks `*pz2`'s whitespace status
   before deciding to break, and retreats **both** `pz1` and `pz2`. The
   `pz2`-is-whitespace branch does **not** check `*pz1` at all before
   breaking (under `IGNORE_CHANGE` it just breaks unconditionally), and
   retreats **only `pz2`**, leaving `pz1` untouched. This is exactly as
   written in the source — I checked it twice against the raw file rather
   than assuming it was a transcription slip. Reproduce it as-is unless you
   have a reason to normalize it (and if you do, that's a deliberate
   behavior change from upstream WinMerge, worth a comment in your port).

4. **§12b: the 3-way dispatcher silently forces `WHITESPACE_COMPARE_ALL`**
   for both of the internal middle-vs-left / middle-vs-right diffs, no matter
   what whitespace option the caller passed to the 3-way entry point. If
   you're porting the 3-way path, this isn't a passthrough bug to "fix" —
   it's what the dispatcher actually does.

5. **§12c: `Compare()`'s whitespace/number handling is a *different,
   weaker* mechanism than the word-diff engine's, despite sharing option
   names.** `Compare()` does literal character substitution/deletion
   (tab→space 1-for-1, or delete all whitespace/digits) with **no run-length
   collapsing**. The word-diff engine's `AreWordsSame` (§5) treats *any*
   whitespace token as equal to *any other* whitespace token regardless of
   length — a much looser equivalence. Don't assume reusing one implementation
   for both purposes in your port will match WinMerge's actual behavior in
   both places — it uses two independently-written interpretations of
   "ignore whitespace changes."

6. **§9's iterator plumbing is an ICU API artifact, not an algorithmic
   requirement.** The original mixes stateful `.next()`/`.previous()` calls
   (continue from wherever the iterator's internal cursor currently sits)
   with explicit-offset `.preceding(x)`/`.following(x)` calls (recompute from
   a given index, ignoring cursor history) — because ICU's `BreakIterator` is
   a single stateful cursor per direction and the code needs to defensively
   re-sync it at certain points. A **stateless** `nextChar(str, idx)` /
   `prevChar(str, idx)` pair (just "give me the boundary before/after this
   index") produces identical results with none of that bookkeeping — this is
   a safe simplification for a port, not a fidelity loss, as long as your
   segmenter can answer boundary queries at an arbitrary offset directly.

7. **Grapheme clusters vs. codepoints is a real fidelity decision, make it
   consciously.** Every character-stepping operation in this file
   (tokenizing, both scans in `ComputeByteDiff`) advances by one ICU
   `UBRK_CHARACTER` boundary — which is a full *extended grapheme cluster*
   (base + combining marks, etc.), not just one UTF-16 code unit and not just
   one codepoint. If your port only steps by codepoint (ignoring combining
   sequences) or by UTF-16 code unit (splitting surrogate pairs), you'll get
   different diff boundaries on text with combining marks, emoji ZWJ
   sequences, etc. Decide up front whether that fidelity matters for your use
   case; if not, codepoint-stepping is a reasonable, much simpler substitute
   for everything else in this file.

8. **Hash must be unsigned 32-bit with wraparound (§4).** `ROL` assumes a
   32-bit word (`sizeof(unsigned)*CHAR_BIT`, true on every mainstream target
   WinMerge ships for). Get this wrong (e.g. using a language's default
   signed/bigint arithmetic without masking) and hashes won't match, which
   only matters if you need bit-for-bit parity with upstream — it does *not*
   break correctness on its own (hash is just a pre-filter, §5 always
   confirms with a real char compare after).

9. **`m_matchblock` and `dp()` are dead in this revision** (§1) — don't
   implement them, don't look for a purpose they don't have here.

10. **The size-cap/timeout fallback is a real behavior, not just an error
    path** (§8). On very large or pathological inputs, WinMerge doesn't
    error out — it silently returns "the entire string is one diff." If your
    port is meant to be a drop-in replacement, keep an equivalent circuit
    breaker with the same fallback shape (even if you tune the actual
    limits), rather than letting the O(NP) engine run unbounded.
