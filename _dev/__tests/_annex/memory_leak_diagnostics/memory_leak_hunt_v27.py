# CudaText memory hunt v27 - v26 crash-fixed + slab-scan fixed + GENERIC mode

# this help to diferenciate between real leaks and fpc garbage retained ram as explained here https://github.com/Alexey-T/CudaText/issues/6533 , it was made by ai and should be consumed by ai. it work similar to the fpc heaptrc but is able to show details about fpc garbage retained ram

# guide:
# 2. start CudaText, open the two big files
# 3. python v27.py --reset --label base     <- seeds the address census
# 4. compare -> wait -> close -> python v27.py --label d1
#    compare -> wait -> close -> python v27.py --label d2
#    compare -> wait -> close -> python v27.py --label d3
#    (d1 names nothing yet - it only stores; d2/d3 do the naming)
# 5. python v27.py --diffall v27_hist.json
# 6. send me v27_out.txt (any run) + v27_hist.json 
#


#
# WHAT v27 FIXES vs v26 (your crash + two silent bugs found while fixing it):
#   FIX 1 (your crash): phase A's "context: N other big private RW regions"
#       line raised "TypeError: not all arguments converted during string
#       formatting" - the % tuple was glued to the second W() literal (which
#       has no placeholders) while the first W() printed raw %d/%.1f. The
#       crash killed the run BEFORE phases 2/C/D/B/E, so you never saw the
#       TFPList census, the frame verdicts, the log strings or the diff.
#   FIX 2 (silent): v26's fixed-chunk slab scans (phase C1, phase B, census)
#       unpacked from the shared read buffer BA, but vmt_class()/tflist_info()
#       /RD() calls INSIDE the loop overwrite BA - everything after the first
#       cache-missed class in a 1 MB slab was silently skipped. v23 was slow
#       (one ReadProcessMemory per block) but correct; v27 keeps the slab
#       speed and snapshots the slab into local bytes() before unpacking.
#   All % format strings are machine-audited (scripts/check_pct_format.py).
#
# WHAT v27 ADDS:
#   --generic   portable accounting for ANY exe (no FPC decoding): totals,
#              region inventory by type, top private regions, --save/--diff.
#   --diff A B  offline diff of two snapshot json files (also: --diff DB.json
#              with ONE file = diff its last two runs). No process needed.
#
# CONTEXT (findings so far; evidence: v23_out1/out2 + the two
# "leak happened / did not happen" runs):
#   - the intermittent ~90-145 MB is FPC chunk STRANDING, not an object leak
#     (census identical in both runs); a live ~3 MB paint buffer
#     (TSharedBitmap field+0x78) pins a big chunk via best-fit carving.
#   - proc_bigalloc (the radical fix) is NOT applied - phase A is therefore
#     intentionally inert on a stock build and only ever prints "none found".
#     Phases 1/2/C/D/B/E/F all work on the STOCK build.
#
# Full-mode phases:
#   phase 1: FPC os-chunk map (pinning candidates: big chunk, tiny busy)
#   phase A: bigalloc magic audit (inert on stock builds)
#   phase 2: survivor decode of big chunks (names what pins them)
#   phase C: TFPList census + element census + owner climb
#   phase C4: TFPList address diff vs previous run + owner climb (v27.2)
#   phase C5: fmMain FComponents tracker - names new owned comps (v27.2)
#   phase D: TEditorFrame orphan detector  -> LEAKED-ORPHAN verdicts
#   phase B: statusbar log-string census ('[HH:MM] ' accumulator)
#   phase E: census snapshot v27_hist.json + diff vs previous run
#   phase F: trim + verdict
#
# v27.2 UPDATE (one-file tool: diffall merged in + two leak namers):
#   - --diffall FLAG integrated (replaces the separate v27_hist_diffall.py):
#     python v27.py --diffall v27_hist.json  diffs EVERY consecutive
#     same-pid pair + prints the per-cycle trend table. One file now.
#   - hist entries now store the FULL class census (all classes, not just
#     the old 15 watch classes) -> --diffall also trends TMenuItem,
#     TChangeLink, TAppMenuProps, TAnchorSide, ... growth per cycle.
#   - new phase C4 (leak namer #1): stores the ADDRESSES of every live
#     TFPList; diffs them vs the previous run of the same pid and
#     owner-climbs each NEW address (find_ptrs + VMT walk) -> prints the
#     owner class + field offset holding each new TFPList. This names the
#     "+2 TFPList per compare cycle" residual leak.
#   - new phase C5 (leak namer #2): locates fmMain via exe-image static
#     roots, reads its owned-components list FComponents (field+0x30,
#     proven = the 5.3 KB TFPList), decodes every entry's class, diffs vs
#     the previous run -> prints the CLASS of every NEW fmMain-owned
#     component. This names the "+1-2 components per cycle" residual leak
#     (post-v29 data: 520->525 over 4 cycles).
#   - phase B literal '%d' format bug fixed (total line printed raw %d).
#
# v27.3 UPDATE (focus-leak edition - names WHO pins the big chunks):
#   Background: round-4 proved all CLASS counts flat, but RAM stays high
#   after close-all when the 2nd file was focused (parsed) before compare.
#   The retention is ~3 MB of non-class data (strings / dynarrays) keeping
#   100+ MB FPC chunks from returning to the OS. Class census cannot see
#   those - so v27.3 names them directly:
#   - new phase C6 (pinner namer): for EVERY >=1MB FPC chunk with busy>0,
#     decodes each busy block (object / ansistring-with-sample-text /
#     dynarray) and owner-climbs it (find_ptrs + VMT walk) -> prints
#     "block size, what it is, which object/field or STATIC ROOT holds it".
#     Stored in hist as "pinners" so --diffall can compare pin sets.
#   - pair_diff_lines now prints an "ALIVE chunk ... pinned by:" section:
#     for chunks present in BOTH runs with busy>0, the named holders from
#     the later run. This is the direct answer to "why is RAM not 70 MB".
#   - trend table gains EControl columns (ECclient/ECthread/ECpub/StrBuf/
#   TATStrings) when present - shows whether lexer parsing ran at all
#   (files > max_size_file_lexer=2 MB default get NO EControl parse).
#
# usage:
#   python v27.py [pid] [--label NAME] [--reset]      full CudaText scan
#   python v27.py <pid> --generic [--save FILE]       any-exe portable scan
#   python v27.py --diff a.json b.json                offline snapshot diff
#   python v27.py --diff v27_hist.json                ...last two runs only
#   python v27.py --diffall v27_hist.json             ALL consecutive pairs
#                                                      + per-cycle trend
# Report -> v27_out.txt  (read-only scanning + final EmptyWorkingSet trim)

import ctypes, sys, os, gc, struct, bisect, json, time, re

try:
    k32   = ctypes.WinDLL('kernel32', use_last_error=True)
    psapi = ctypes.WinDLL('psapi',   use_last_error=True)
    WIN   = True
except (AttributeError, OSError, ValueError):   # non-Windows: --diff only
    k32 = psapi = None
    WIN   = False
CQ = ctypes.c_ulonglong
HP = ctypes.c_void_p

def say(*a): print(*a, flush=True)

if WIN:
    k32.GetCurrentProcess.restype  = HP
    k32.GetCurrentProcessId.restype= ctypes.c_ulong
    k32.ReadProcessMemory.argtypes = [HP, HP, HP, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
    k32.ReadProcessMemory.restype  = ctypes.c_int
    k32.VirtualQueryEx.argtypes    = [HP, HP, ctypes.c_void_p, ctypes.c_size_t]
    k32.VirtualQueryEx.restype     = ctypes.c_size_t
    k32.GetProcessHeaps.argtypes   = [ctypes.c_ulong, HP]
    k32.GetProcessHeaps.restype    = ctypes.c_ulong
    k32.HeapCompact.argtypes       = [HP, ctypes.c_ulong]
    k32.HeapCompact.restype        = ctypes.c_size_t
    k32.SetProcessWorkingSetSize.argtypes = [HP, ctypes.c_size_t, ctypes.c_size_t]
    k32.SetProcessWorkingSetSize.restype  = ctypes.c_int
    k32.OpenProcess.argtypes       = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    k32.OpenProcess.restype        = HP
    k32.CloseHandle.argtypes       = [HP]
    k32.CloseHandle.restype        = ctypes.c_int
    k32.CreateToolhelp32Snapshot.argtypes = [ctypes.c_ulong, ctypes.c_ulong]
    k32.CreateToolhelp32Snapshot.restype  = HP
    k32.Process32FirstW.argtypes   = [HP, ctypes.c_void_p]   # (HANDLE, LPPROCESSENTRY32W) - 2 args
    k32.Process32NextW.argtypes    = [HP, ctypes.c_void_p]
    psapi.GetProcessMemoryInfo.argtypes=[HP, HP, ctypes.c_ulong]
    psapi.GetProcessMemoryInfo.restype = ctypes.c_int
    psapi.EnumProcessModules.argtypes=[HP, ctypes.POINTER(HP), ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong)]
    psapi.EnumProcessModules.restype = ctypes.c_ulong
    psapi.GetModuleFileNameExW.argtypes=[HP, HP, ctypes.c_wchar_p, ctypes.c_ulong]
    psapi.GetModuleFileNameExW.restype= ctypes.c_ulong
    psapi.GetModuleInformation.argtypes=[HP, HP, HP, ctypes.c_ulong]
    psapi.GetModuleInformation.restype = ctypes.c_int

class MBI(ctypes.Structure):
    _fields_=[("BaseAddress",CQ),("AllocationBase",CQ),("AllocationProtect",ctypes.c_ulong),
              ("__a",ctypes.c_ulong),("RegionSize",CQ),("State",ctypes.c_ulong),
              ("Protect",ctypes.c_ulong),("Type",ctypes.c_ulong),("__b",ctypes.c_ulong)]
class MODINFO(ctypes.Structure):
    _fields_=[("BaseOfDll",CQ),("SizeOfImage",ctypes.c_ulong),("_p",ctypes.c_ulong),("EntryPoint",HP)]
class PE(ctypes.Structure):
    _fields_=[("dwSize",ctypes.c_ulong),("cntUsage",ctypes.c_ulong),
              ("th32ProcessID",ctypes.c_ulong),("th32DefaultHeapID",ctypes.c_size_t),
              ("th32ModuleID",ctypes.c_ulong),("th32Threads",ctypes.c_ulong),
              ("th32ParentProcessID",ctypes.c_ulong),("pcPriClassBase",ctypes.c_long),
              ("dwFlags",ctypes.c_ulong),("szExeFile",ctypes.c_wchar*260)]

# proc_bigalloc.pas header constants (keep in sync with the unit)
BIGALLOC_MAGIC1 = 0x7EB16B16C0DA7E77
BIGALLOC_MAGIC2 = 0x5EEDFACE0BADC0DE
BIGALLOC_HDRSZ  = 32
BIGALLOC_THR    = 4*1024*1024

# ---------------- target selection ----------------
def find_cudatext_pids():
    own = k32.GetCurrentProcessId()
    out=[]
    snap = k32.CreateToolhelp32Snapshot(2, 0)     # TH32CS_SNAPPROCESS
    if not snap or snap == HP(-1).value: return out
    pe = PE(); pe.dwSize = ctypes.sizeof(PE)
    p = ctypes.byref(pe)
    ok = k32.Process32FirstW(snap, p)
    while ok:
        try: name = pe.szExeFile or ''
        except Exception: name = ''
        if name and name.lower().startswith('cudatext') and pe.th32ProcessID != own:
            out.append((pe.th32ProcessID, name))
        ok = k32.Process32NextW(snap, p)
    k32.CloseHandle(snap)
    out.sort(key=lambda t: (t[1].lower()!='cudatext.exe', t[0]))
    return out

LABEL  = None
RESET  = False
TARGET_PID = None
GENERIC = False
SAVE_PATH = None
DIFF_FILES = None
DIFFALL_FILES = None
_args = [a for a in sys.argv[1:]]
_i = 0
while _i < len(_args):
    a = _args[_i]
    if a == '--label' and _i+1 < len(_args):
        LABEL = _args[_i+1]; _i += 2; continue
    if a == '--reset':
        RESET = True; _i += 1; continue
    if a == '--generic':
        GENERIC = True; _i += 1; continue
    if a == '--save' and _i+1 < len(_args):
        SAVE_PATH = _args[_i+1]; _i += 2; continue
    if a == '--diffall':
        DIFFALL_FILES = _args[_i+1:]; _i = len(_args); continue
    if a == '--diff':
        DIFF_FILES = _args[_i+1:]; _i = len(_args); continue
    try:
        TARGET_PID = int(a, 0); _i += 1; continue
    except ValueError:
        _i += 1
# (target selection + dispatch moved to the bottom, after all defs -
#  --diff must be able to run before any Win32 handle is opened)

# ---------------- single zero-copy read buffer ----------------
BA_SZ  = 0x110000
BA     = bytearray(BA_SZ)
_CB    = (ctypes.c_char * BA_SZ).from_buffer(BA)
_RDN   = ctypes.c_size_t(0)
BA_ADDR= ctypes.addressof(_CB)

def RD(addr, n):
    if addr is None or n <= 0 or n > BA_SZ: return None
    if not k32.ReadProcessMemory(PROC, HP(addr), _CB, n, ctypes.byref(_RDN)):
        return None
    return _RDN.value

def qw(addr):
    if RD(addr, 8) != 8: return None
    return struct.unpack_from('<Q', BA, 0)[0]

def dw(addr):
    if RD(addr, 4) != 4: return None
    return struct.unpack_from('<i', BA, 0)[0]

def meminfo():
    buf = ctypes.create_string_buffer(80)
    struct.pack_into('<I', buf, 0, 80)
    if not psapi.GetProcessMemoryInfo(PROC, ctypes.byref(buf), 80): return {}
    g = lambda o: struct.unpack_from('<Q', buf, o)[0]
    return dict(peakws=g(8), ws=g(16), qnp=g(48), commit=g(56),
                peakcommit=g(64), private=g(72))

def writable(p): return (p & 0xFF) in (4, 8, 0x40, 0x80) and not (p & 0x100)

def vm_runs():
    out=[]; addr=0; m=MBI()
    while addr < 0x7FFFFFFFFFFF:
        if k32.VirtualQueryEx(PROC, HP(addr), ctypes.byref(m), ctypes.sizeof(m))==0: break
        out.append((m.BaseAddress, m.RegionSize, m.State, m.Protect, m.Type, m.AllocationBase))
        addr += m.RegionSize
        if m.RegionSize==0: break
    return out

def mod_list():
    arr=(HP*4096)(); need=ctypes.c_ulong(0)
    if not psapi.EnumProcessModules(PROC, arr, ctypes.sizeof(arr), ctypes.byref(need)):
        return []
    n=min(need.value//ctypes.sizeof(HP), 4096); out=[]; mi=MODINFO()
    for i in range(n):
        h=arr[i]
        if h is None: continue
        wb=ctypes.create_unicode_buffer(600)
        psapi.GetModuleFileNameExW(PROC, h, wb, 600)
        if psapi.GetModuleInformation(PROC, h, ctypes.byref(mi), ctypes.sizeof(mi)):
            out.append((mi.BaseOfDll, mi.SizeOfImage, wb.value.split("\\")[-1]))
    out.sort()
    return out

MODS=[]
IMAGES=[]
PYIMAGES=[]
def where(va):
    for b,s,n in MODS:
        if b<=va<b+s: return "%s+0x%x" % (n, va-b)
    return "0x%x" % va
def in_image(va):
    for lo,hi,n in IMAGES:
        if lo<=va<hi: return n
    return None
def in_pyimage(va):
    for lo,hi,n in PYIMAGES:
        if lo<=va<hi: return True
    return False

# ---------------- FPC VMT class naming (proven in v23) ----------------
_VMT_CACHE={}
def _name_ok(bs):
    if not (3 <= len(bs) <= 63): return False
    for c in bs:
        if not (48<=c<=57 or 65<=c<=90 or 97<=c<=122 or c==95): return False
    return 65<=bs[0]<=90 or 97<=bs[0]<=122

def vmt_class(v):
    """FPC 3.2.2 x64 VMT: +0x00 instance size, +0x08 NEGATED instance size,
       +0x10 parent VMT, +0x18 -> PShortString class name."""
    if not v or not (0x10000 < v < 0x7FFFFFFFFFFF): return None
    key = "0x%x" % v
    if key in _VMT_CACHE: return _VMT_CACHE[key]
    name=None
    if in_image(v) and RD(v, 0x20) == 0x20:
        isz  = struct.unpack_from('<Q', BA, 0)[0]
        isz2 = struct.unpack_from('<q', BA, 8)[0]
        par  = struct.unpack_from('<Q', BA, 0x10)[0]
        nptr = struct.unpack_from('<Q', BA, 0x18)[0]
        if 8 <= isz <= 0x8000000 and (isz2 == -isz or 0 <= isz2 <= 0x8000000):
            if par == 0 or in_image(par):
                if nptr and 0x10000 < nptr < 0x7FFFFFFFFFFF and (RD(nptr, 64) or 0) >= 2:
                    ln = BA[0]
                    if 3 <= ln <= 63 and _name_ok(BA[1:1+ln]):
                        name = BA[1:1+ln].decode('ascii','replace')
    _VMT_CACHE[key]=name
    return name

def vmt_ancestors(v, maxhops=10):
    chain=[]
    while v and 0x10000 < v < 0x7FFFFFFFFFFF and len(chain) < maxhops:
        nm = vmt_class(v)
        if not nm: break
        chain.append(nm)
        if RD(v, 0x18) != 0x18: break
        v = struct.unpack_from('<Q', BA, 0x10)[0]
    return chain

def owner_class_of(h, window=0x2000):
    """scan backward from a field address for a qword that VMT-validates"""
    lo = h - window
    if lo < 0x10000: lo = 0x10000
    n = RD(lo, h - lo)
    if not n: return None, None
    for off in range(n - 8, -1, -8):
        v = struct.unpack_from('<Q', BA, off)[0]
        if v and in_image(v):
            nm = vmt_class(v)
            if nm: return nm, h - (lo + off)
    return None, None

# ---------------- FPC 3.2.2 chunk walking (proven in v23) ----------------
def walk_var_blocks(O, end):
    H = O + 0x38; chunks=[]; steps=0
    while H < end and steps < 400000:
        if RD(H+0x10, 8) != 8: break
        raw = struct.unpack_from('<Q', BA, 0)[0]
        sz = raw & ~0xF
        fl = raw & 0xF
        if sz < 0x20 or H + sz > end + 0x2000: break
        chunks.append((H, sz, fl))
        H += sz
        steps += 1
    return chunks

def fpc_walk(rbase, rsize):
    O = rbase + 0x40
    if RD(O, 0x30) != 0x30: return None
    tsize, nf, pa, na, used, fls = struct.unpack_from('<6Q', BA, 0)
    if abs(tsize - rsize) > 0x18000: return None
    if used != 0xFFFFFFFFFFFFFFFF: return None
    owner_ok = bool(fls) and RD(fls, 16) is not None
    chunks = walk_var_blocks(O, rbase + rsize)
    if not chunks: return None
    tiled = bool(chunks[0][2] & 8) and (rsize - (chunks[-1][0]+chunks[-1][1])) < 0x2000
    return dict(O=O, tsize=tsize, used=used, fls=fls, region=True, kind='var',
                base=rbase, size=rsize, owner_ok=owner_ok,
                chunks=chunks, tiled=tiled)

def try_toschunk(O):
    if not (0x10000 < O < 0x7FFFFFFFFFFF): return None
    if RD(O, 0x30) != 0x30: return None
    tsize, nf, pa, na, used, fls = struct.unpack_from('<6Q', BA, 0)
    if tsize < 0x2000 or tsize > 0x40000000 or (tsize & 0xF): return None
    if used == 0xFFFFFFFFFFFFFFFF: kind='var'
    elif 0 <= used <= 10_000_000: kind='fixed'
    else: return None
    if fls and not (0x10000 < fls < 0x7FFFFFFFFFFF): return None
    return dict(O=O, tsize=tsize, used=used, fls=fls, kind=kind, region=False)

def fixed_stride(O):
    raw = qw(O + 0x38)
    if raw is None: return None
    if (raw >> 12) != 0x38: return None
    S = raw & 0xFE0
    if S < 32 or S > 544 or (S & 0x1F): return None
    return S

# ---------------- needle holder hunt (proven in v23) ----------------
def find_ptrs(addrs):
    keys    = ["0x%x" % a for a in addrs]
    needles = {"0x%x" % a: (a, struct.pack('<Q', a)) for a in addrs}
    selfmask = [(id(nd) & ~0xFFF, (id(nd) & ~0xFFF) + 0x1000)
                for (a, nd) in needles.values()]
    raw = {k: [] for k in keys}
    runs = vm_runs()
    for (rs, sz, st, pr, ty, ab) in runs:
        if st != 0x1000 or not writable(pr) or sz < 0x1000: continue
        if rs <= BA_ADDR < rs+sz: continue
        kind = 'image' if ty == 0x1000000 else ('private' if ty == 0x20000 else 'stack/other')
        off = 0
        while off < sz:
            n = min(0xFF000, sz-off)
            got = RD(rs+off, n+8)
            if got:
                for k, (a, nd) in needles.items():
                    pos = BA.find(nd, 0, got)
                    while pos >= 0:
                        if pos % 8 == 0:
                            hv = rs+off+pos
                            if not any(lo <= hv < hi for (lo,hi) in selfmask):
                                raw[k].append((hv, kind, in_image(hv)))
                        pos = BA.find(nd, pos+1, got)
            off += n
    def pylong_phantom(hv, val):
        # hv points at PyLong ob_digit[0]; works for 1- AND 2-digit values
        nd = qw(hv-8)
        if nd not in (1, 2): return False
        if not in_pyimage(qw(hv-16) or 0): return False
        if nd == 1:
            return val < 0x40000000
        if not (0x40000000 <= val < (1 << 60)): return False
        if RD(hv, 8) != 8: return False
        d0 = struct.unpack_from('<I', BA, 0)[0]
        d1 = struct.unpack_from('<I', BA, 4)[0]
        return d0 == (val & 0x3FFFFFFF) and d1 == (val >> 30)
    def dictentry_phantom(hv, val):
        # compact-dict entry: hv = me_key/me_value slot; the OTHER slot at
        # hv+8 (or hv-8) holds a PyLong whose digits equal val.
        # NOTE: extract d0/d1 IMMEDIATELY after RD - any qw() call clobbers BA
        for other in (hv+8, hv-8):
            k = qw(other)
            if not k or not (0x10000 < k < 0x7FFFFFFFFFFF): continue
            t = qw(k+8)
            if t is None or not in_pyimage(t): continue
            nd = qw(k+16)
            if nd not in (1, 2): continue
            if RD(k+24, 8) != 8: continue
            d0 = struct.unpack_from('<I', BA, 0)[0]
            d1 = struct.unpack_from('<I', BA, 4)[0]
            if nd == 1 and val < 0x40000000 and d0 == val:
                return True
            if nd == 2 and 0x40000000 <= val < (1 << 60):
                if d0 == (val & 0x3FFFFFFF) and d1 == (val >> 30):
                    return True
        return False
    res={}
    for k in keys:
        val = needles[k][0]
        out=[]
        for (hv, kind, mod) in raw[k]:
            if pylong_phantom(hv, val):
                out.append((hv, 'pylong-remnant', None))
            elif dictentry_phantom(hv, val):
                out.append((hv, 'dictentry-remnant', None))
            else:
                out.append((hv, kind, mod))
        res[k] = sorted(set(out))
    return res

def nonzero_ratio(p, psz):
    lim = min(psz, 0x20000)
    if RD(p, lim) != lim: return None
    nz = 0
    for i in range(0, lim, 64):
        if BA[i]: nz += 1
    return nz / max(1, (lim+63)//64)

# AnsiString block layout (FPC 3.2.2 x64, proven in v23 decode_payload):
#   block data p: [cp:2|es:2|pad:4][ref:8][len:8][chars...]
def ansi_header_at(p):
    """returns (cp, es, ref, ln) if p looks like an AnsiString block start"""
    if RD(p, 24) != 24: return None
    A = struct.unpack_from('<Q', BA, 0)[0]
    B = struct.unpack_from('<q', BA, 8)[0]
    C = struct.unpack_from('<q', BA, 16)[0]
    cp, es = A & 0xFFFF, (A >> 16) & 0xFFFF
    if es in (1, 2, 4) and 0 <= B < 10_000_000 and 4 < C < 100_000_000:
        return (cp, es, B, C)
    return None

TSMSG_RE = re.compile(rb'\[\d\d:\d\d\] ')

# ---------------- v27 new: bigalloc mapping detection ----------------
def scan_bigalloc(runs):
    """private RW committed regions >= BIGALLOC_THR whose first 16 bytes are
    the proc_bigalloc header magic. Returns [(base, regionsize, usable)]."""
    out=[]
    for (rs, sz, st, pr, ty, ab) in runs:
        if st != 0x1000 or ty != 0x20000 or not writable(pr): continue
        if sz < BIGALLOC_THR: continue
        if RD(rs, 0x18) != 0x18: continue
        m1 = struct.unpack_from('<Q', BA, 0)[0]
        m2 = struct.unpack_from('<Q', BA, 8)[0]
        if m1 == BIGALLOC_MAGIC1 and m2 == BIGALLOC_MAGIC2:
            usable = struct.unpack_from('<Q', BA, 0x10)[0]  # TBigHeader.Size
            out.append((rs, sz, usable))
    return out

# ---------------- v27 new: TFPList layout helpers ----------------
# FPC Classes.TFPList x64 (S=32): +0x08 FList ptr, +0x10 FCount(int32),
# +0x14 FCapacity(int32). Verified against v23's out2: the 5 KB array was
# held by TFPList @0x10fd680 field+0x8.
def tflist_info(blk):
    """blk = object data start (after VMT). Returns (flist, count, cap)."""
    if RD(blk, 24) != 24: return None
    fl  = struct.unpack_from('<Q', BA, 8)[0]
    cnt = struct.unpack_from('<i', BA, 16)[0]
    cap = struct.unpack_from('<i', BA, 20)[0]
    return (fl, cnt, cap)

# ---------------- v27 new: snapshot store / diff ----------------
def snap_path():
    try: here = os.path.dirname(os.path.abspath(__file__))
    except NameError: here = "."
    return os.path.join(here, "v27_hist.json")

def snap_load():
    p = snap_path()
    if RESET and os.path.isfile(p):
        try: os.remove(p)
        except OSError: pass
        say("v27: previous snapshot removed (--reset)")
    if os.path.isfile(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            say("v27: snapshot read failed (%r) - starting fresh" % (e,))
    return {"runs": []}

def snap_save(db, entry):
    db["runs"].append(entry)
    try:
        with open(snap_path(), "w", encoding="utf-8") as f:
            json.dump(db, f, indent=1)
    except Exception as e:
        say("v27: snapshot write failed: %r" % (e,))

CENSUS_CLASSES = ["TEditorFrame", "TATSynEdit", "TTimer", "TFPList",
                  "TSharedBitmap", "TATStatus", "TATStatusData",
                  "TATStrings", "TATFoldRangeList", "TRegExpr",
                  "TControlFont", "TATScrollbar", "TAppNotifThread",
                  "TCustomDockForm", "TFormDummy"]
# (v27.2: hist entries now store the FULL census; the list above is kept
#  only as documentation of the original watch set.)

def pair_diff_lines(prev, cur):
    """v27.2 unified per-pair diff - used by --diff, --diffall and the live
    phase E print. Format matches the old v27_hist_diffall.py output."""
    MB_ = 1024.0 * 1024.0
    out = []
    def fmt_mb(v): return '%.1f' % (v / MB_)
    out.append("A: [%s | pid %s | %s | mode %s]" %
               (prev.get("label") or "-", prev.get("pid"),
                prev.get("time") or "-", prev.get("mode") or "full"))
    out.append("B: [%s | pid %s | %s | mode %s]" %
               (cur.get("label") or "-", cur.get("pid"),
                cur.get("time") or "-", cur.get("mode") or "full"))
    out.append("commit %s -> %s MB (delta %+.1f)   WS %s -> %s MB" %
               (fmt_mb(prev["commit"]), fmt_mb(cur["commit"]),
                (cur["commit"] - prev["commit"]) / MB_,
                fmt_mb(prev["ws"]), fmt_mb(cur["ws"])))
    # census deltas - every class present in either run
    cen_a, cen_b = prev.get("census") or {}, cur.get("census") or {}
    rows = []
    for k in set(cen_a) | set(cen_b):
        va, vb = cen_a.get(k, 0), cen_b.get(k, 0)
        if vb != va:
            rows.append((k, va, vb, vb - va))
    rows.sort(key=lambda r: -abs(r[3]))
    for k, va, vb, dd in rows:
        flag = '   <<< GROWS (unbounded leak if per-cycle)' if dd > 0 else ''
        out.append('  census %-24s %5d -> %5d  (%+d)%s' % (k, va, vb, dd, flag))
    # chunks >= 1 MB: NEW / GONE
    pa = {c[0]: c for c in prev.get("chunks", [])}
    ca = {c[0]: c for c in cur.get("chunks", [])}
    for c in sorted((ca[k] for k in set(ca) - set(pa)), key=lambda c: -c[1]):
        if c[1] >= MB_:
            out.append('  NEW chunk 0x%x  %s MB  busy %s MB in %d block(s)' %
                       (c[0], fmt_mb(c[1]), fmt_mb(c[2]), c[3]))
    for c in sorted((pa[k] for k in set(pa) - set(ca)), key=lambda c: -c[1]):
        if c[1] >= MB_:
            out.append('  GONE chunk 0x%x  %s MB  (fully emptied -> returned to OS)' %
                       (c[0], fmt_mb(c[1])))
    # v27.3: who keeps the still-alive big chunks from returning to the OS
    pin_b = {c[0]: c for c in (cur.get("pinners") or [])}
    kept = [k for k in (set(pa) & set(ca)) if ca[k][1] >= MB_ and ca[k][2] > 0]
    for k in sorted(kept, key=lambda x: -ca[x][1]):
        rec = pin_b.get(k)
        if not rec or not rec[3]:
            continue
        out.append('  ALIVE chunk 0x%x  %s MB  busy %s MB - pinned by:' %
                   (k, fmt_mb(ca[k][1]), fmt_mb(ca[k][2])))
        for blk in sorted(rec[3], key=lambda b: -b[1])[:4]:
            _p, _s, _what, _hold = blk
            out.append('      %9d B  %-38s <- %s' %
                       (_s, _what, "; ".join(_hold[:2]) if _hold else '?'))
    for c in sorted((ca[k] for k in set(ca) - set(pa)), key=lambda c: -c[1]):
        if c[1] >= MB_ and c[2] > 0:
            rec = pin_b.get(c[0])
            if rec and rec[3]:
                out.append('  (NEW chunk 0x%x, pinned by:)' % c[0])
                for blk in sorted(rec[3], key=lambda b: -b[1])[:3]:
                    _p, _s, _what, _hold = blk
                    out.append('      %9d B  %-38s <- %s' %
                               (_s, _what, "; ".join(_hold[:2]) if _hold else '?'))
    # bigalloc (proc_bigalloc fix) mappings if recorded
    pb = {x[0]: x for x in prev.get("bigalloc", [])}
    cb = {x[0]: x for x in cur.get("bigalloc", [])}
    for k in sorted(set(cb) - set(pb)):
        out.append("  NEW bigalloc mapping 0x%x  %.1f MB" % (k, cb[k][1] / 1e6))
    for k in sorted(set(pb) - set(cb)):
        out.append("  GONE bigalloc mapping 0x%x  %.1f MB  (returned to OS - fix working)" %
                   (k, pb[k][1] / 1e6))
    # statusbar log strings
    la, lb = prev.get("logstrings") or [], cur.get("logstrings") or []
    if la or lb:
        out.append('  statusbar log strings: %d -> %d  (max len %d -> %d)' %
                   (len(la), len(lb),
                    max([x[1] for x in la], default=0),
                    max([x[1] for x in lb], default=0)))
    # TFPList inventory (top-10 by array size, as stored in hist)
    ta, tb = prev.get("tflists") or [], cur.get("tflists") or []
    if ta or tb:
        from collections import Counter
        sa = Counter(x[1] for x in ta)
        sb = Counter(x[1] for x in tb)
        deltas = [(sz, sb.get(sz, 0) - sa.get(sz, 0))
                  for sz in set(sa) | set(sb)
                  if sb.get(sz, 0) != sa.get(sz, 0)]
        if deltas:
            txt = ', '.join('%+d x %dB(%d slots)' % (dd, sz, sz // 8)
                            for sz, dd in sorted(deltas, key=lambda t: -abs(t[1])))
            out.append('  tflist sizes: %d -> %d lists; %s' % (len(ta), len(tb), txt))
        else:
            out.append('  tflist sizes: %d -> %d lists (same size mix)' % (len(ta), len(tb)))
    # v27.2: TFPList address turnover + owner-climb attribution (phase C4)
    aa, ab = prev.get("tflist_addrs"), cur.get("tflist_addrs")
    if aa and ab is not None:
        sa, sb = set(aa), set(ab)
        out.append('  TFPList addresses: %d -> %d  (+%d new / -%d gone)' %
                   (len(sa), len(sb), len(sb - sa), len(sa - sb)))
    for rec in (cur.get("tflist_new") or []):
        a_, cnt_, cap_, arr_, owners_ = rec
        out.append('  new TFPList 0x%x  count=%d cap=%d array=%d B' %
                   (a_, cnt_, cap_, arr_))
        for s_ in owners_:
            out.append('      <- %s' % s_)
    # v27.2: fmMain owned-components turnover + new classes (phase C5)
    fa, fb = prev.get("fmcomps"), cur.get("fmcomps")
    if fa is not None and fb is not None:
        sa = set(c[0] for c in fa)
        sb = set(c[0] for c in fb)
        out.append('  fmMain owned-components: %d -> %d  (+%d new / -%d gone)' %
                   (len(sa), len(sb), len(sb - sa), len(sa - sb)))
    for rec in (cur.get("fmcomps_new") or []):
        out.append('  NEW fmMain-owned: %s @0x%x' % (rec[1], rec[0]))
    return out

def snap_diff(prev, cur):
    """kept for compatibility - v27.2 delegates to the unified printer"""
    return pair_diff_lines(prev, cur)

# ---------------- v27 new: generic (any-exe) mode + offline diff ----------------
def generic_report(W, m0, runs):
    """portable accounting for ANY process - no FPC/VMT decoding at all"""
    W("--- generic mode: portable process accounting ---")
    W("(no FPC heap walk, no class names: that decoding is FPC-specific;")
    W(" run WITHOUT --generic on CudaText for object-level attribution)")
    exe = MODS[0][2] if MODS else "?"
    groups = {}
    priv_rw = []
    for (rs, sz, st, pr, ty, ab) in runs:
        if st == 0x1000:
            key = {0x20000:'private', 0x1000000:'image',
                   0x40000:'mapped'}.get(ty, 'other-committed')
        elif st == 0x2000:
            key = 'reserved'
        else:
            key = 'free/other'
        c, b = groups.get(key, (0, 0))
        groups[key] = (c+1, b+sz)
        if st == 0x1000 and ty == 0x20000 and writable(pr):
            priv_rw.append((rs, sz))
    W("exe=%s  pid=%d" % (exe, TARGET_PID))
    for key in ('private', 'image', 'mapped', 'other-committed',
                'reserved', 'free/other'):
        if key in groups:
            c, b = groups[key]
            W("  %-15s %6d region(s)  %10.1f MB" % (key, c, b/1e6))
    W("  top private RW regions:")
    for (rs, sz) in sorted(priv_rw, key=lambda t: -t[1])[:12]:
        W("    0x%-14x %8.2f MB" % (rs, sz/1e6))
    entry = dict(
        mode='generic',
        label=LABEL or time.strftime("%Y-%m-%d %H:%M:%S"),
        time=time.strftime("%Y-%m-%d %H:%M:%S"),
        pid=TARGET_PID, exe=exe,
        ws=m0.get('ws', 0), commit=m0.get('commit', 0),
        private=m0.get('private', 0),
        regions=[[b, s] for (b, s) in priv_rw if s >= 0x10000],
    )
    if SAVE_PATH:
        try:
            with open(SAVE_PATH, "w", encoding="utf-8") as f:
                json.dump(entry, f, indent=1)
            W("snapshot saved -> %s" % SAVE_PATH)
            W("leak protocol: --save snap2.json after the suspect action,")
            W("then:  python v27.py --diff %s snap2.json" % SAVE_PATH)
        except Exception as e:
            W("snapshot save failed: %r" % (e,))
    else:
        W("(leak protocol: re-run with --save snap1.json BEFORE the suspect")
        W(" action and --save snap2.json AFTER, then --diff the two files)")
    W("")

def generic_diff(a, b):
    out = []
    for k in ("private", "commit", "ws"):
        av, bv = a.get(k, 0), b.get(k, 0)
        out.append("%-8s %8.1f -> %8.1f MB  (delta %+.1f)" %
                   (k, av/1e6, bv/1e6, (bv-av)/1e6))
    ra = {tuple(r): r for r in a.get("regions", [])}
    rb = {tuple(r): r for r in b.get("regions", [])}
    new  = sorted((rb[k] for k in set(rb)-set(ra)), key=lambda r: -r[1])
    gone = sorted((ra[k] for k in set(ra)-set(rb)), key=lambda r: -r[1])
    out.append("new private RW regions: %d  (%.1f MB)" %
               (len(new), sum(s for _, s in new)/1e6))
    out.extend("  + 0x%-14x %8.2f MB" % (bb, ss/1e6) for (bb, ss) in new[:12])
    out.append("gone private RW regions: %d  (%.1f MB)" %
               (len(gone), sum(s for _, s in gone)/1e6))
    out.extend("  - 0x%-14x %8.2f MB" % (bb, ss/1e6) for (bb, ss) in gone[:12])
    out.append("net private committed in tracked regions: %+.1f MB" %
               ((sum(s for _, s in new) - sum(s for _, s in gone))/1e6))
    out.append("reading: many small +regions = heap growth / small-object leak;")
    out.append("        one big +region that stays = a buffer kept alive;")
    out.append("        commit up with no new regions = growth inside regions.")
    return out

def _snap_entry_from(path):
    with open(path, "r", encoding="utf-8") as f:
        d = json.load(f)
    if isinstance(d, dict) and isinstance(d.get("runs"), list):
        if not d["runs"]:
            raise ValueError("%s has no runs" % path)
        return d["runs"][-1]
    if not isinstance(d, dict):
        raise ValueError("%s is not a snapshot json" % path)
    return d

def _snap_tag(e):
    return "[%s | pid %s | %s | mode %s]" % (e.get("label") or "-",
                                             e.get("pid"),
                                             e.get("time") or "-",
                                             e.get("mode") or "full")

def cmd_diff(files):
    try:
        if len(files) == 1:
            with open(files[0], "r", encoding="utf-8") as f:
                db = json.load(f)
            runs_ = db.get("runs") if isinstance(db, dict) else None
            if not runs_ or len(runs_) < 2:
                say("v27: %s has too few runs to self-diff" % files[0])
                return 2
            A, B = runs_[-2], runs_[-1]
            say("=== v27 diff: last two runs of %s ===" % files[0])
        elif len(files) == 2:
            A = _snap_entry_from(files[0]); B = _snap_entry_from(files[1])
            say("=== v27 diff ===")
        else:
            say("v27: --diff takes one or two snapshot files")
            return 2
    except Exception as e:
        say("v27: diff failed: %r" % (e,))
        return 2
    if A.get("pid") and B.get("pid") and A.get("pid") != B.get("pid"):
        say("NOTE: different pids - numbers are not directly comparable.")
    fn = generic_diff if "generic" in (A.get("mode"), B.get("mode")) else snap_diff
    if fn is generic_diff:
        # generic_diff has no A/B header of its own; snap_diff (=pair_diff_lines) does
        say("A: %s" % _snap_tag(A))
        say("B: %s" % _snap_tag(B))
    for l in fn(A, B):
        say(l)
    if len(files) == 1:
        say("")
        say("(tip: --diffall %s diffs EVERY consecutive pair + trend table)" % files[0])
    return 0

def cmd_diffall(files):
    """v27.2: diff EVERY consecutive same-pid pair in one hist file +
    per-cycle trend table (what v27_hist_diffall.py did, now built in)."""
    if len(files) != 1:
        say("v27: --diffall takes exactly one hist file, e.g.")
        say("      python v27.py --diffall v27_hist.json")
        return 2
    fn = files[0]
    try:
        with open(fn, "r", encoding="utf-8") as f:
            db = json.load(f)
        runs_ = db.get("runs") if isinstance(db, dict) else db
        runs_ = [r for r in runs_ if r.get("mode") == "full"]
    except Exception as e:
        say("v27: --diffall failed: %r" % (e,))
        return 2
    if len(runs_) < 2:
        say("v27: need >= 2 full-mode runs in %s (found %d)" % (fn, len(runs_)))
        return 2
    pairs = [(runs_[i], runs_[i + 1]) for i in range(len(runs_) - 1)
             if runs_[i].get("pid") == runs_[i + 1].get("pid")]
    skipped = (len(runs_) - 1) - len(pairs)
    say("=== v27 hist diff-all: %d consecutive pair(s) in %s ===" % (len(pairs), fn))
    if skipped:
        say("(%d pair(s) skipped: pid change = fresh app session)" % skipped)
    say("")
    for i, (a, b) in enumerate(pairs):
        say("--- pair %d/%d ---" % (i + 1, len(pairs)))
        for l in pair_diff_lines(a, b):
            say(l)
        say("")
    # ---- per-cycle trend table ----
    say("=== per-cycle trend ===")
    MB_ = 1024.0 * 1024.0
    wide = any("TMenuItem" in (r.get("census") or {}) for r in runs_)
    hasfm = any(r.get("fmcomps") is not None for r in runs_)
    ecwide = any("TecClientSyntAnalyzer" in (r.get("census") or {}) for r in runs_)
    hdr = ("label", "commit", "d_commit", "TEdFrame", "TATSynEdit",
           "TATGapItem", "TTimer", "TFPList")
    fmt = "%-10s %8s %9s %9s %10s %10s %6s %7s"
    if wide:
        hdr += ("TMenuItem", "TChangeLink")
        fmt += " %9s %11s"
    if hasfm:
        hdr += ("fmOwn",)
        fmt += " %6s"
    if ecwide:
        hdr += ("ECclient", "ECthread", "ECpub", "StrBuf", "TATStrings")
        fmt += " %8s %8s %6s %7s %10s"
    hdr += ("pins>=1MB",)
    fmt += " %9s"
    say(fmt % hdr)
    prev = None
    for r in runs_:
        cen = r.get("census") or {}
        pins = [c for c in r.get("chunks", []) if c[1] >= MB_]
        pin_mb = sum(c[1] for c in pins) / MB_
        d = "" if prev is None else "%+.1f" % ((r["commit"] - prev) / MB_)
        row = [r.get("label") or "-", "%.1f" % (r["commit"] / MB_), d,
               cen.get("TEditorFrame", 0), cen.get("TATSynEdit", 0),
               cen.get("TATGapItem", 0), cen.get("TTimer", 0),
               cen.get("TFPList", 0)]
        if wide:
            row += [cen.get("TMenuItem", 0), cen.get("TChangeLink", 0)]
        if hasfm:
            fm = r.get("fmcomps")
            row += [len(fm) if fm is not None else "-"]
        if ecwide:
            row += [cen.get("TecClientSyntAnalyzer", 0),
                    cen.get("TecParserThread", 0),
                    cen.get("TecPublicData", 0),
                    cen.get("TATStringBuffer", 0),
                    cen.get("TATStrings", 0)]
        row += ["%.0fMB" % pin_mb]
        say(fmt % tuple(row))
        prev = r["commit"]
    return 0

# ---------------- v27 new: frame referrer classification ----------------
TABCTRL_HINTS = ("TATTabs", "TATPages", "TATGroups", "TATPagesSimple",
                 "TATPanelNoFlicker")

def decode_payload(p, psz):
    """v23's proven busy-block decoder (ansistring / object / dynarray)"""
    if RD(p, 64) != 64: return dict(kind='unreadable', p=p, psz=psz)
    out = dict(kind='raw', p=p, psz=psz)
    A = struct.unpack_from('<Q', BA, 0)[0]
    B = struct.unpack_from('<q', BA, 8)[0]
    C = struct.unpack_from('<q', BA, 16)[0]
    cls = vmt_class(A) if A else None
    if cls:
        out.update(kind='object', cls=cls); return out
    cp, es = A & 0xFFFF, (A >> 16) & 0xFFFF
    if es in (1,2,4) and 0 <= B < 1000000 and 4 < C <= psz-24:
        if RD(p+24, 80):
            asc = ''.join(chr(BA[i]) if 32<=BA[i]<127 else '.' for i in range(56))
            out.update(kind='ansistring', len=C, ref=B, cp=cp, es=es, sample=asc[:48])
            return out
    if 0 <= A < 1000000 and B >= 3:
        cnt = B+1
        base = psz-16
        for esz in {base // cnt, -(-base // cnt)}:
            if 0 < esz <= 64 and 0 <= base - esz*cnt <= 64:
                data = p+0x10
                out.update(kind='dynarray', count=cnt, esz=esz, data=data, ref=A)
                if esz == 8 and RD(data, 24):
                    out.update(ptr_samples=[struct.unpack_from('<Q',BA,j*8)[0]
                                            for j in range(3)
                                            if struct.unpack_from('<Q',BA,j*8)[0]])
                return out
    if RD(p, 48):
        out.update(ascii=''.join(chr(BA[i]) if 32<=BA[i]<127 else '.' for i in range(48)),
                   head=bytes(BA[0:32]).hex())
    return out

# ---------------- main ----------------
try: HERE = os.path.dirname(os.path.abspath(__file__))
except NameError: HERE = "."
OUT = os.path.join(HERE, "v27_out.txt") if os.path.isdir(HERE) else "v27_out.txt"

def main():
    rep=open(OUT,"w")
    def W(s=""):
        rep.write(s+"\n"); rep.flush()
    try:
        m0=meminfo()
        runs=vm_runs()
        MODS[:] = mod_list()
        IMAGES[:] = [(b, b+s, n) for (b, s, n) in MODS]
        PYIMAGES[:] = [(b, b+s, n) for (b, s, n) in MODS if n.lower().startswith("python")]
        def _mod_off(va):
            for b,s,n in MODS:
                if b<=va<b+s: return "%s+0x%x" % (n, va-b)
            return "0x%x" % va
        walktot = sum(sz for (rs,sz,st,pr,ty,ab) in runs if st==0x1000)
        W("=== v27: name the culprits + verify the fix ===")
        W("target pid=%d  mode=%s  label=%s  python dlls=%d" %
          (TARGET_PID, "IN-PROCESS (polluted!)" if INPROC else "external",
           LABEL or "-", len(PYIMAGES)))
        W("now : WS=%.1f commit=%.1f private=%.1f MB" %
          (m0.get('ws',0)/1e6, m0.get('commit',0)/1e6, m0.get('private',0)/1e6))
        W("peak: WS=%.1f commit=%.1f MB" %
          (m0.get('peakws',0)/1e6, m0.get('peakcommit',0)/1e6))
        W("modules=%d committed-walk=%.1f MB" % (len(MODS), walktot/1e6))
        W("")

        if GENERIC:
            generic_report(W, m0, runs)
            say("v27 generic finished -> " + OUT)
            return

        # ---- phase 1: FPC os-chunk map (v23 1a/1b) ----
        W("--- phase 1: FPC os-chunks ---")
        fpcs=[]
        for (rs,sz,st,pr,ty,ab) in sorted(runs):
            if st!=0x1000 or ty!=0x20000 or not writable(pr) or sz < 0x8000:
                continue
            w = fpc_walk(rs, sz)
            if w:
                w["busy"]=[(h,s,fl) for (h,s,fl) in w["chunks"] if fl & 2]
                fpcs.append(w)
        allc = {}
        for w in fpcs:
            allc["0x%x"%w["O"]] = w
        arenas = {}
        def note_arena(fls):
            if fls and 0x10000 < fls < 0x7FFFFFFFFFFF:
                k="0x%x"%fls
                if k not in arenas: arenas[k]=fls
        for w in fpcs: note_arena(w["fls"])
        seeds=[]
        for w in fpcs: seeds.append(w["O"])
        for k,fls in list(arenas.items()):
            head = qw(fls + 0xA0)          # tfreelists.oslist_all
            if head: seeds.append(head)
        qi=0; seen=set(seeds and ["0x%x"%s for s in seeds] or [])
        while seeds and qi < 4096:
            O = seeds.pop(0); qi += 1
            k = "0x%x" % O
            if k in allc:
                rec = allc[k]
            else:
                rec = try_toschunk(O)
                if rec is None: continue
                if rec["kind"] == 'var':
                    rec["chunks"] = walk_var_blocks(O, O - 0x40 + rec["tsize"])
                    if not rec["chunks"]: continue
                    rec["busy"]=[(h,s,fl) for (h,s,fl) in rec["chunks"] if fl & 2]
                else:
                    rec["S"] = fixed_stride(O)
                    if not rec["S"]: continue
                    rec["chunks"]=[]; rec["busy"]=[]
                allc[k] = rec
                note_arena(rec["fls"])
            for link in (0x10, 0x18):
                nxt = qw(O + link)
                if nxt and 0x10000 < nxt < 0x7FFFFFFFFFFF:
                    nk = "0x%x" % nxt
                    if nk not in seen:
                        seen.add(nk); seeds.append(nxt)
        FIXED_FREE=set()
        for k,fls in arenas.items():
            for i in range(17):
                head = qw(fls + 8 + i*8)
                steps=0
                while head and 0x10000 < head < 0x7FFFFFFFFFFF and steps < 60000:
                    FIXED_FREE.add("0x%x" % head)
                    nxt = qw(head + 8)
                    if nxt is None or nxt == head: break
                    head = nxt; steps += 1
        CL = sorted(allc.values(), key=lambda r: r["O"])
        CL_O = [r["O"] for r in CL]
        def chunk_of(addr):
            i = bisect.bisect_right(CL_O, addr) - 1
            if i < 0: return None
            r = CL[i]
            if addr >= r["O"] - 0x40 + r["tsize"]: return None
            return r
        def fpc_classify(addr):
            r = chunk_of(addr)
            if r is None: return None
            if addr < r["O"] + 0x38: return ('fpc-toschunk', r["O"], 0)
            if r["kind"] == 'var':
                for (h,s,fl) in r["chunks"]:
                    if h <= addr - 0x18 < h + s:
                        return (('fpc-busy-var' if fl&2 else 'fpc-FREE-var(stale)'),
                                h + 0x18, s - 0x18)
                return ('fpc-var-gap', r["O"], 0)
            S = r["S"]
            k = (addr - r["O"] - 0x40) // S
            pmc = r["O"] + 0x38 + k*S
            raw = qw(pmc)
            if raw is None or (raw >> 12) != (pmc - r["O"]):
                return ('fpc-fixed?', r["O"], S)
            free = "0x%x" % pmc in FIXED_FREE
            return (('fpc-FREE-fixed(stale)' if free else 'fpc-busy-fixed'),
                    pmc + 8, S)
        W("total FPC os-chunks mapped: %d  (var=%d fixed=%d)  arenas=%d  fixed-free=%d" %
          (len(allc), sum(1 for r in allc.values() if r["kind"]=='var'),
           sum(1 for r in allc.values() if r["kind"]=='fixed'), len(arenas), len(FIXED_FREE)))
        bigch=[r for r in allc.values() if r["kind"]=='var' and r["tsize"]>=0x90000]
        for r in sorted(bigch, key=lambda x:-x["tsize"])[:12]:
            bsum=sum(s for (_,s,_) in r.get("busy",[]))
            W("  chunk 0x%-12x %8.2f MB busy %7.3f MB in %d block(s)" %
              (r["O"]-0x40, r["tsize"]/1e6, bsum/1e6, len(r.get("busy",[]))))
        W("")

        # ---- phase A: bigalloc mapping detection (fix verification) ----
        W("--- phase A: proc_bigalloc OS mappings (magic $%x/$%x) ---" %
          (BIGALLOC_MAGIC1, BIGALLOC_MAGIC2))
        bigalloc = scan_bigalloc(runs)
        if bigalloc:
            W("  ACTIVE: %d mapping(s), total %.1f MB:" %
              (len(bigalloc), sum(sz for (_,sz,_) in bigalloc)/1e6))
            for (b, sz, usable) in sorted(bigalloc):
                W("    0x%-12x region %8.2f MB  payload %8.2f MB" %
                  (b, sz/1e6, usable/1e6))
            W("  -> these are the diff texts / batch arrays living OUTSIDE the")
            W("     FPC heap; they must disappear from this list after the")
            W("     compare tab is closed (that is the fix working).")
        else:
            W("  none found. Either this build predates the proc_bigalloc fix,")
            W("  or no >=4 MiB allocation is live right now (run during a")
            W("  compare to see the ~90 MB texts here).")
        # other big private regions (context only)
        others=[]
        fpc_chunk_spans = [(r["O"]-0x40, r["O"]-0x40+r["tsize"])
                           for r in allc.values()]
        ba_bases = set(b for (b,_,_) in bigalloc)
        for (rs, sz, st, pr, ty, ab) in runs:
            if st!=0x1000 or ty!=0x20000 or not writable(pr) or sz < BIGALLOC_THR: continue
            if rs in ba_bases: continue
            if any(b <= rs < b+s for (b,s,_) in bigalloc): continue
            if any(lo <= rs < hi for (lo,hi) in fpc_chunk_spans): continue
            others.append((rs, sz))
        if others:
            W("  (context: %d other big private RW region(s), %.1f MB total -\n"
              "   python/LCL/stacks, not FPC, not bigalloc)" %
              (len(others), sum(s for _,s in others)/1e6))
        W("")

        # ---- phase 2: survivor decode of big chunks (v23 continuity) ----
        W("--- phase 2: survivor decode (FPC var chunks >= 576 KB) ---")
        survivors=[]
        for r in sorted(bigch, key=lambda x:x["O"]):
            if not r.get("busy"): continue
            W("chunk 0x%x:" % (r["O"]-0x40))
            for (h,s,fl) in sorted(r["busy"], key=lambda c:-c[1]):
                p = h+0x18
                dec = decode_payload(p, s-0x18)
                nzr = nonzero_ratio(p, s-0x18)
                line = "  alloc=0x%x (+0x%07x) %8.3f MB fl=0x%x nz=%.2f %-14s" % \
                       (p, p-(r["O"]-0x40), (s-0x18)/1e6, fl,
                        nzr if nzr is not None else -1, dec["kind"])
                if dec["kind"]=='object':
                    line += " %s" % dec["cls"]
                elif dec["kind"]=='ansistring':
                    line += " len=%d ref=%d cp=%d %r" % (dec["len"], dec["ref"], dec["cp"], dec.get("sample",""))
                elif dec["kind"]=='dynarray':
                    line += " count=%d esz=%d" % (dec["count"], dec["esz"])
                else:
                    line += " ascii=%r" % dec.get("ascii","")[:48]
                W(line)
                dec["chunk"]=r
                survivors.append(dec)
            W("")
        W("survivors total: %d (=%.3f MB busy across %d big chunks)" %
          (len(survivors), sum(d["psz"] for d in survivors)/1e6, len(bigch)))
        W("")

        # ---- phase C1: TFPList census (objects are fixed-class S=32) ----
        W("--- phase C1: TFPList census ---")
        tflists=[]           # dicts: obj, fl, cnt, cap, arrsz, arrdata
        flist_arr_ranges=[]  # (arrdata_lo, arrdata_hi, tflist_obj)
        for r in CL:
            if r["kind"] != 'fixed': continue
            if r["S"] != 32: continue
            nblk = (r["tsize"] - 0x40) // 32
            # slab-read the whole fixed chunk for speed
            lo = r["O"] + 0x38
            hi = r["O"] + 0x38 + nblk*32
            pos = lo
            while pos < hi:
                n = min(0x100000, hi-pos)
                if RD(pos, n) is None: break
                base = pos
                slab = bytes(BA[:n])   # v27: immune to BA clobbering below
                # walk blocks inside this slab
                k0 = (pos - r["O"] - 0x38) // 32
                kend = k0 + n//32
                for k in range(k0, kend):
                    off = (r["O"] + 0x38 + k*32) - base
                    raw = struct.unpack_from('<Q', slab, off)[0]
                    pmc = r["O"] + 0x38 + k*32
                    if (raw >> 12) != (pmc - r["O"]): continue
                    if "0x%x" % pmc in FIXED_FREE: continue
                    v = struct.unpack_from('<Q', slab, off+8)[0]
                    if not v or not in_image(v): continue
                    if vmt_class(v) != 'TFPList': continue
                    blk = pmc + 8
                    inf = tflist_info(blk)
                    if not inf: continue
                    fl, cnt, cap = inf
                    ent = dict(obj=blk, fl=fl, cnt=cnt, cap=cap, arrsz=0, arrdata=None)
                    if fl and 0x10000 < fl < 0x7FFFFFFFFFFF:
                        cls = fpc_classify(fl)
                        if cls and cls[0] in ('fpc-busy-var','fpc-busy-fixed'):
                            ent["arrdata"], ent["arrsz"] = cls[1], cls[2]
                            flist_arr_ranges.append((cls[1], cls[1]+cls[2], blk))
                    tflists.append(ent)
                pos += n
        W("live TFPList objects: %d  (with FList array resolved: %d)" %
          (len(tflists), sum(1 for t in tflists if t["arrdata"])))
        top = sorted(tflists, key=lambda t:-t["arrsz"])[:10]
        for t in top:
            W("  TFPList@0x%-10x count=%-6d cap=%-6d FList=0x%x array=%d B" %
              (t["obj"], t["cnt"], t["cap"], t["fl"], t["arrsz"]))
        W("")

        # ---- phase C2: element census of the top TFPLists ----
        W("--- phase C2: what the biggest TFPLists hold ---")
        for t in top[:5]:
            if not t["arrdata"] or t["cnt"] <= 0: continue
            n = min(t["cnt"], 96)
            if RD(t["arrdata"], n*8) != n*8: continue
            entries = [struct.unpack_from('<Q', BA, i*8)[0] for i in range(n)]
            tally={}
            for e in entries:
                lab = "nil" if not e else "int/small"
                if e and 0x10000 < e < 0x7FFFFFFFFFFF:
                    v = qw(e)
                    nm = vmt_class(v) if v else None
                    if nm: lab = nm
                    else:
                        ah = ansi_header_at(e-24)
                        if ah and ah[0] in (65001, 0, 1252):
                            lab = "ansistring(len=%d)" % ah[3]
                        else:
                            cls = fpc_classify(e)
                            lab = "raw" if (cls and cls[0].startswith('fpc-busy')) else "ptr"
                tally[lab] = tally.get(lab, 0) + 1
            W("  TFPList@0x%x (count=%d, array=%d B) first %d entries:" %
              (t["obj"], t["cnt"], t["arrsz"], n))
            for lab, c in sorted(tally.items(), key=lambda kv:-kv[1])[:8]:
                W("      %-28s %3d" % (lab, c))
        W("")

        # ---- phase C3: dedicated owner climb for the top-3 arrays ----
        W("--- phase C3: owner climb of the biggest TFPLists (v23's unfinished mission) ---")
        def obj_of_holder(hv):
            cls = fpc_classify(hv)
            if not cls or cls[0] not in ('fpc-busy-var','fpc-busy-fixed'):
                return None
            obj, size = cls[1], cls[2]
            v = qw(obj)
            if v and in_image(v):
                nm = vmt_class(v)
                if nm:
                    return dict(obj=obj, cls=nm, size=size, field=hv-obj)
            c2, back = owner_class_of(hv)
            if c2:
                return dict(obj=hv-back, cls=c2, size=None, field=back)
            return dict(obj=obj, cls=None, size=size, field=hv-obj)
        for t in top[:3]:
            name = "TFPList@0x%x (array %d B, count %d)" % (t["obj"], t["arrsz"], t["cnt"])
            res = find_ptrs([t["obj"]])
            hs = res.get("0x%x"%t["obj"], [])
            gen = [(h,kd,m) for (h,kd,m) in hs if kd not in ('pylong-remnant','dictentry-remnant')]
            W("  %s <- %d genuine referrer(s)" % (name, len(gen)))
            level=0; frontier=[]
            for (hv, kd, m) in gen[:12]:
                o = obj_of_holder(hv)
                if o:
                    W("      0x%x -> %s @0x%x (S=%s field+0x%x)" %
                      (hv, o["cls"] or "?", o["obj"],
                       ("0x%x"%o["size"]) if o["size"] else "?", o["field"]))
                    frontier.append(o)
            seenobj=set([t["obj"]])
            while frontier and level < 4:
                level += 1
                batch=[o["obj"] for o in frontier[:8]]
                res2=find_ptrs(batch)
                nextf=[]
                for o in frontier[:8]:
                    hs2 = res2.get("0x%x"%o["obj"], [])
                    gen2=[(h,kd,m) for (h,kd,m) in hs2 if kd not in ('pylong-remnant','dictentry-remnant')]
                    nm2 = "%s@0x%x" % (o["cls"] or "obj", o["obj"])
                    if not gen2:
                        W("   %s : no referrer  <<< ORPHAN / static-root check needed" % nm2)
                        continue
                    imgs=[(h,m) for (h,kd,m) in gen2 if kd=='image']
                    for (h,m) in imgs[:2]:
                        W("   %s <- STATIC ROOT %s" % (nm2, _mod_off(h)))
                    for (hv, kd, m) in gen2[:8]:
                        o2 = obj_of_holder(hv)
                        if o2 and o2["obj"] not in seenobj:
                            seenobj.add(o2["obj"])
                            W("   %s <- %s @0x%x (S=%s field+0x%x)" %
                              (nm2, o2["cls"] or "?", o2["obj"],
                               ("0x%x"%o2["size"]) if o2["size"] else "?", o2["field"]))
                            nextf.append(o2)
                frontier = nextf
            W("")

        # ---- phase C4 (v27.2): NEW TFPLists vs previous run - leak namer ----
        W("--- phase C4: TFPLists new vs previous snapshot (the +2/cycle namer) ---")
        tflist_addrs = sorted(t["obj"] for t in tflists)
        tflist_new = None
        prev_entry = None
        for _r in reversed(snap_db.get("runs", [])):
            if _r.get("pid") == TARGET_PID and _r.get("mode") == "full":
                prev_entry = _r; break
        _prev_addrs = prev_entry.get("tflist_addrs") if prev_entry else None
        if not _prev_addrs:
            W("  (no previous TFPList address census for pid %d - storing it now;" % TARGET_PID)
            W("   from your NEXT labeled run on, every NEW TFPList gets named here)")
        else:
          try:
            cur_set = set(tflist_addrs); prev_set = set(_prev_addrs)
            new = sorted(cur_set - prev_set)
            gone = prev_set - cur_set
            W("  TFPList turnover vs previous run: %d new, %d gone (live total %d)" %
              (len(new), len(gone), len(cur_set)))
            if new:
                tflist_new = []
                batch = new[:10]
                resp = find_ptrs(batch)
                for a in batch:
                    t = None
                    for tt in tflists:
                        if tt["obj"] == a:
                            t = tt; break
                    cnt = t["cnt"] if t else -1
                    cap = t["cap"] if t else -1
                    arrsz = t["arrsz"] if t else 0
                    owners = []
                    hs = resp.get("0x%x" % a, [])
                    gen = [(h, kd, m) for (h, kd, m) in hs
                           if kd not in ('pylong-remnant', 'dictentry-remnant')]
                    for (hv, kd, m) in gen[:6]:
                        o = obj_of_holder(hv)
                        if o:
                            owners.append("%s@0x%x field+0x%x%s" %
                                          (o["cls"] or "?", o["obj"], o["field"],
                                           "  [exe image - STATIC ROOT]" if kd == 'image' else ""))
                        else:
                            owners.append("holder@0x%x (%s)" % (hv, kd))
                    if not gen:
                        owners.append("(no genuine referrer - ORPHANED, nothing points at it)")
                    W("  NEW TFPList@0x%-10x count=%-5d cap=%-5d array=%d B" %
                      (a, cnt, cap, arrsz))
                    for s in owners:
                        W("      <- %s" % s)
                    tflist_new.append([a, cnt, cap, arrsz, owners])
                if len(new) > len(batch):
                    W("  ... and %d more new TFPList(s) not climbed (cap is 10 per run)" %
                      (len(new) - len(batch)))
            else:
                W("  no new TFPList objects since previous run")
          except Exception as e:
            W("  TFPList turnover/climb failed: %r (non-fatal)" % (e,))
        W("")

        # ---- phase C5 (v27.2): fmMain owned-components tracker ----
        W("--- phase C5: fmMain owned-components tracker (field+0x30) ---")
        fmcomps = None
        fmcomps_new = None
        try:
            fm_found = []
            fm_mod = None
            for (_mb, _ms, _mn) in MODS:
                if _mn.lower().startswith("cudatext") and _mn.lower().endswith(".exe"):
                    fm_mod = (_mb, _ms); break
            if fm_mod is None and MODS:
                fm_mod = (MODS[0][0], MODS[0][1])
            if fm_mod:
                b0, s0 = fm_mod
                for (rs, sz, st, pr, ty, ab) in runs:
                    if st != 0x1000 or ty != 0x1000000 or not writable(pr):
                        continue
                    if not (b0 <= rs < b0 + s0):
                        continue
                    off = 0
                    while off < sz:
                        n = min(0xFF000, sz - off)
                        if RD(rs + off, n):
                            slab = bytes(BA[:n])
                            for o in range(0, n - 8, 8):
                                v = struct.unpack_from('<Q', slab, o)[0]
                                if not v or in_image(v):
                                    continue
                                if not (0x10000 < v < 0x7FFFFFFFFFFF):
                                    continue
                                vm = qw(v)
                                if vm and vmt_class(vm) == 'TfmMain':
                                    if v not in fm_found:
                                        fm_found.append(v)
                        off += n
            if not fm_found:
                W("  TfmMain not found via exe-image static roots - tracker skipped")
            else:
                W("  TfmMain @ %s" % ", ".join("0x%x" % x for x in fm_found[:4]))
                X = None
                for fm in fm_found[:4]:
                    _x = qw(fm + 0x30)
                    if _x:
                        _vm = qw(_x)
                        if _vm and vmt_class(_vm) == 'TFPList':
                            X = _x
                            break
                if X is None:
                    W("  field+0x30 is not a TFPList (layout change?) - tracker skipped")
                else:
                    fl, cnt, cap = tflist_info(X)
                    comps = []
                    nn = min(cnt, 4000)
                    pos = 0
                    while pos < nn * 8:
                        m = min(0x100000, nn * 8 - pos)
                        if RD(fl + pos, m) is None:
                            break
                        slab2 = bytes(BA[:m])
                        for i in range(pos // 8, (pos + m) // 8):
                            e = struct.unpack_from('<Q', slab2, i * 8 - pos)[0]
                            if not e or not (0x10000 < e < 0x7FFFFFFFFFFF):
                                continue
                            vm2 = qw(e)
                            nm2 = vmt_class(vm2) if vm2 else None
                            comps.append([e, nm2 or "?"])
                        pos += m
                    comps.sort()
                    fmcomps = comps
                    W("  FComponents: %d entries (cap %d)" % (len(comps), cap))
                    tally = {}
                    for (_, nm2) in comps:
                        tally[nm2] = tally.get(nm2, 0) + 1
                    for nm2, c in sorted(tally.items(), key=lambda kv: -kv[1])[:8]:
                        W("      %-24s %5d" % (nm2, c))
                    if prev_entry is not None and prev_entry.get("fmcomps") is not None:
                        prev_addrs = set(c[0] for c in prev_entry["fmcomps"])
                        cur_addrs = set(c[0] for c in comps)
                        newc = [c for c in comps if c[0] not in prev_addrs]
                        gonec = len(prev_addrs - cur_addrs)
                        W("  turnover vs previous run: +%d new / -%d gone" %
                          (len(newc), gonec))
                        if newc:
                            fmcomps_new = newc
                            t2 = {}
                            for (_, nm2) in newc:
                                t2[nm2] = t2.get(nm2, 0) + 1
                            W("  *** NEW fmMain-owned components (leak suspects): ***")
                            for nm2, c in sorted(t2.items(), key=lambda kv: -kv[1]):
                                W("      %-24s %+d" % (nm2, c))
                            for (a2, nm2) in newc[:12]:
                                W("      0x%-10x %s" % (a2, nm2))
                        else:
                            W("  no new fmMain-owned components since previous run")
                    else:
                        W("  (no previous fmcomps for this pid - storing it now;")
                        W("   from your NEXT labeled run on, new components get named)")
        except Exception as e:
            W("  fmMain tracker failed: %r (non-fatal)" % (e,))
        W("")

        # ---- phase D: TEditorFrame census + orphan verdict ----
        W("--- phase D: TEditorFrame census / orphan detector ---")
        frames=[]
        for r in CL:
            if r["kind"] != 'var': continue
            for (h,s,fl) in r.get("busy", []):
                p = h+0x18
                v = qw(p)
                nm = vmt_class(v) if v else None
                if nm == 'TEditorFrame':
                    frames.append((p, s-0x18))
        W("live TEditorFrame objects: %d" % len(frames))
        # which frame-lists hold TEditorFrame elements? (AppFrameList1/2/Deleting)
        frame_lists=set()
        for t in tflists:
            if not t["arrdata"] or t["cnt"] <= 0: continue
            n = min(t["cnt"], 64)
            if RD(t["arrdata"], n*8) != n*8: continue
            for i in range(n):
                e = struct.unpack_from('<Q', BA, i*8)[0]
                if e and vmt_class(qw(e)) == 'TEditorFrame':
                    frame_lists.add(t["obj"])
                    break
        W("TFPLists holding TEditorFrame elements (frame-lists): %d %s" %
          (len(frame_lists), sorted("0x%x"%x for x in frame_lists)[:6]))
        frame_rows=[]
        # one batched needle pass for all frames (a full-VM walk per frame
        # would be ~24x slower)
        fbatch = [fp for (fp, _) in frames[:24]]
        fresp = find_ptrs(fbatch) if fbatch else {}
        for (fp, fsz) in frames[:24]:
            hs = fresp.get("0x%x"%fp, [])
            gen = [(h,kd,m) for (h,kd,m) in hs if kd not in ('pylong-remnant','dictentry-remnant')]
            refs=[]; in_list=False; tabctl=False
            for (hv, kd, m) in gen:
                # hit inside a frame-list array?
                for (lo, hi, tobj) in flist_arr_ranges:
                    if lo <= hv < hi:
                        in_list = True
                        refs.append("in FList of TFPList@0x%x" % tobj)
                        break
                else:
                    o = obj_of_holder(hv)
                    if o and o["cls"]:
                        refs.append("%s@0x%x+0x%x" % (o["cls"], o["obj"], o["field"]))
                        if any(h in (o["cls"] or "") for h in TABCTRL_HINTS):
                            tabctl = True
            verdict = "LIVE" if (in_list or tabctl) else ("LEAKED-ORPHAN" if gen else "UNREFERENCED")
            frame_rows.append((fp, verdict))
            W("  TEditorFrame@0x%-10x S=0x%-6x %-14s refs: %s" %
              (fp, fsz, verdict, "; ".join(refs[:6]) if refs else "(none)"))
        W("")

        # ---- phase B: statusbar log-string census ----
        W("--- phase B: statusbar log strings ('[HH:MM] ' accumulator) ---")
        logstrings=[]
        for r in CL:
            if r["kind"] == 'var':
                for (h,s,fl) in r.get("busy", []):
                    p = h+0x18
                    ah = ansi_header_at(p)
                    if not ah or ah[3] < 8: continue
                    cp, es, ref, ln = ah
                    n = min(ln, 4096)
                    if RD(p+24, n) != n: continue
                    if not TSMSG_RE.match(BA[0:16]): continue
                    nmsg = len(TSMSG_RE.findall(BA[:n]))
                    trunc = ">" if n < ln else ""
                    chunk = chunk_of(p)
                    cinfo = ""
                    if chunk:
                        cbase = chunk["O"]-0x40
                        cbusy = sum(s2 for (_,s2,_) in chunk.get("busy",[]))
                        cinfo = " in chunk 0x%x (%.1f MB, busy %.3f MB)" % \
                                (cbase, chunk["tsize"]/1e6, cbusy/1e6)
                    logstrings.append((p, ln, nmsg))
                    W("  str@0x%-10x len=%-6d msgs%s=%-3d ref=%d cp=%d%s" %
                      (p, ln, trunc, nmsg, ref, cp, cinfo))
            else:
                S = r["S"]
                if S < 64: continue
                nblk = (r["tsize"] - 0x40) // S
                # slab-read the fixed chunk, probe blocks locally (an RD per
                # block would be tens of thousands of syscalls per chunk)
                lo = r["O"] + 0x38
                hi = r["O"] + 0x38 + nblk*S
                pos = lo
                while pos < hi:
                    n = min(0x100000, hi-pos)
                    if RD(pos, n) is None: break
                    base = pos
                    slab = bytes(BA[:n])   # v27: immune to BA clobbering below
                    k0 = (pos - r["O"] - 0x38) // S
                    kend = k0 + n//S
                    for k in range(k0, kend):
                        off = (r["O"] + 0x38 + k*S) - base
                        raw = struct.unpack_from('<Q', slab, off)[0]
                        pmc = r["O"] + 0x38 + k*S
                        if (raw >> 12) != (pmc - r["O"]): continue
                        if "0x%x" % pmc in FIXED_FREE: continue
                        p = pmc + 8
                        A = struct.unpack_from('<Q', slab, off+8)[0]
                        B = struct.unpack_from('<q', slab, off+16)[0] if off+24 <= n else None
                        C = struct.unpack_from('<q', slab, off+24)[0] if off+32 <= n else None
                        cp, es = A & 0xFFFF, (A >> 16) & 0xFFFF
                        if es not in (1, 2, 4): continue
                        if B is None or not (0 <= B < 10_000_000): continue
                        if C is None or not (0 <= C < S-24): continue
                        # candidate: read the chars and check the '[HH:MM] ' prefix
                        nn = min(C, 4096)
                        if RD(p+24, nn) != nn: continue
                        if not TSMSG_RE.match(BA[0:16]): continue
                        nmsg = len(TSMSG_RE.findall(BA[:nn]))
                        trunc = ">" if nn < C else ""
                        logstrings.append((p, C, nmsg))
                        W("  str@0x%-10x len=%-6d msgs%s=%-3d ref=%d cp=%d (fixed S=%d)" %
                          (p, C, trunc, nmsg, B, cp, S))
                    pos += n
        if logstrings:
            W("  total %d log string(s), %d bytes; the long ones are the joined" %
              (len(logstrings), sum(x[1] for x in logstrings)))
            W("  statusbar-cell HINT (AppStringsTrailingText of the last")
            W("  MaxStatusbarMessages=35 messages) - it is rebuilt on EVERY")
            W("  MsgStatus call, so during a compare it lands in the active")
            W("  big FPC chunk and pins it after the compare frees the buffer.")
            W("  With the proc_bigalloc fix the big buffers are no longer FPC")
            W("  chunks, so this string becomes harmless wherever it lands.")
        else:
            W("  none found (no MsgStatus with a timestamp prefix is alive).")
        W("")

        # holders of the biggest log string (needle hunt)
        if logstrings:
            bp, bln, bm = max(logstrings, key=lambda x: x[1])
            W("--- phase B2: holders of the biggest log string (len %d) ---" % bln)
            res = find_ptrs([bp])
            hs = res.get("0x%x"%bp, [])
            gen = [(h,kd,m) for (h,kd,m) in hs if kd not in ('pylong-remnant','dictentry-remnant')]
            W("  0x%x <- %d genuine hit(s)" % (bp, len(gen)))
            for (hv, kd, m) in gen[:12]:
                o = obj_of_holder(hv)
                if o:
                    W("      0x%x -> %s @0x%x (S=%s field+0x%x)" %
                      (hv, o["cls"] or "?", o["obj"],
                       ("0x%x"%o["size"]) if o["size"] else "?", o["field"]))
                else:
                    cls = fpc_classify(hv)
                    W("      0x%x -> %s" % (hv, cls[0] if cls else kd))
            W("")

        # ---- phase C6 (v27.3): pinner namer ----
        # For every >=1MB FPC chunk with busy>0: decode each busy block and
        # owner-climb it. This names WHO keeps the chunk (and therefore the
        # RAM) alive after close-all. Result is also stored in the hist entry
        # ("pinners") so --diffall prints it per pair.
        W("--- phase C6: pinner namer (what keeps each >=1MB chunk alive) ---")
        pinners = []
        PIN_CH_MIN = 0x100000      # 1 MB chunk threshold (matches pins>=1MB)
        PIN_BLK_TOP = 10           # name top-N blocks per chunk by size
        PIN_BLK_MIN = 0x40000      # ...plus any block >= 256 KB
        PIN_MAX_NEEDLES = 60       # global cap for the single find_ptrs pass
        cand = [r for r in allc.values()
                if r["kind"] == 'var' and r["tsize"] >= PIN_CH_MIN and r.get("busy")]
        cand.sort(key=lambda x: -x["tsize"])
        sel6 = []
        seen_n = set()
        for r in cand:
            blocks6 = sorted(r["busy"], key=lambda b: -b[1])
            picked = list(blocks6[:PIN_BLK_TOP])
            picked += [b for b in blocks6[PIN_BLK_TOP:] if b[1] >= PIN_BLK_MIN]
            sel6.append((r, picked))
        needles6 = []
        for (r, picked) in sel6:
            for (h, s, fl) in picked:
                p = h + 0x18
                if p not in seen_n:
                    seen_n.add(p)
                    needles6.append(p)
        allowed6 = set(needles6[:PIN_MAX_NEEDLES])
        resp6 = find_ptrs(sorted(allowed6)) if allowed6 else {}
        ptot6 = 0.0
        try:
            for (r, picked) in sel6:
                cbase = r["O"] - 0x40
                bsum = sum(s for (_, s, _) in r["busy"])
                ptot6 += r["tsize"]
                W("chunk 0x%-12x %8.2f MB busy %8.3f MB in %d block(s)" %
                  (cbase, r["tsize"] / 1e6, bsum / 1e6, len(r["busy"])))
                rec_blocks = []
                for (h, s, fl) in picked:
                    p = h + 0x18
                    dec = decode_payload(p, s - 0x18)
                    if dec["kind"] == 'object':
                        what = "object %s" % dec.get("cls")
                    elif dec["kind"] == 'ansistring':
                        what = "str len=%d %s" % (dec.get("len", 0),
                                                   (dec.get("sample") or "")[:36])
                    elif dec["kind"] == 'dynarray':
                        what = "dynarray n=%d esz=%d" % (dec.get("count", 0),
                                                          dec.get("esz", 0))
                    else:
                        what = dec["kind"]
                    if p in allowed6:
                        hs = resp6.get("0x%x" % p, [])
                        gen = [(hv, kd, m) for (hv, kd, m) in hs
                               if kd not in ('pylong-remnant', 'dictentry-remnant')]
                    else:
                        gen = []
                    holders = []
                    for (hv, kd, m) in gen[:4]:
                        if kd == 'image':
                            holders.append("STATIC ROOT %s" % _mod_off(hv))
                        else:
                            o6 = obj_of_holder(hv)
                            if o6 and o6.get("cls"):
                                holders.append("%s@0x%x+0x%x" %
                                               (o6["cls"], o6["obj"], o6["field"]))
                            else:
                                c6 = fpc_classify(hv)
                                holders.append("ptr@0x%x(%s)" %
                                               (hv, c6[0] if c6 else kd))
                    if not allowed6 or p not in allowed6:
                        holders.append("(scan cap reached)")
                    elif not gen:
                        holders.append("(no referrer)")
                    W("  blk 0x%-10x %9d B  %-38s <- %s" %
                      (p, s - 0x18, what, "; ".join(holders[:3])))
                    rec_blocks.append([p, s - 0x18, what, holders[:6]])
                pinners.append([cbase, r["tsize"], bsum, rec_blocks])
        except Exception as e6:
            W("  (pinner scan partial: %r)" % (e6,))
        W("total >=1MB chunk space (pinned set): %.1f MB" % (ptot6 / 1e6))
        W("(run-to-run: chunks alive in BOTH runs with busy>0 are the RAM that")
        W(" cannot return to the OS; the holders above name WHO keeps them;")
        W(" strings print their text, dynarrays their element count/size)")
        W("")

        # ---- full census (fixed slab + var) for the snapshot ----
        census={}
        for r in CL:
            if r["kind"] == 'fixed':
                S = r["S"]
                nblk = (r["tsize"] - 0x40) // S
                lo = r["O"] + 0x38
                hi = r["O"] + 0x38 + nblk*S
                pos = lo
                while pos < hi:
                    n = min(0x100000, hi-pos)
                    if RD(pos, n) is None: break
                    base = pos
                    slab = bytes(BA[:n])   # v27: immune to BA clobbering below
                    k0 = (pos - r["O"] - 0x38) // S
                    kend = k0 + n//S
                    for k in range(k0, kend):
                        off = (r["O"] + 0x38 + k*S) - base
                        raw = struct.unpack_from('<Q', slab, off)[0]
                        pmc = r["O"] + 0x38 + k*S
                        if (raw >> 12) != (pmc - r["O"]): continue
                        if "0x%x" % pmc in FIXED_FREE: continue
                        v = struct.unpack_from('<Q', slab, off+8)[0]
                        if v and in_image(v):
                            nm = vmt_class(v)
                            if nm: census[nm] = census.get(nm, 0) + 1
                    pos += n
            else:
                for (h,s,fl) in r.get("busy", []):
                    v = qw(h + 0x18)
                    if v and in_image(v):
                        nm = vmt_class(v)
                        if nm: census[nm] = census.get(nm, 0) + 1
        W("--- census (live instances by class, top 25) ---")
        for nm, c in sorted(census.items(), key=lambda kv:-kv[1])[:25]:
            W("  %-24s %5d" % (nm, c))
        W("")

        # ---- phase E: snapshot + diff ----
        W("--- phase E: snapshot%s ---" % ((" label=" + LABEL) if LABEL else ""))
        entry = dict(
            mode='full',
            label=LABEL or ("run%d" % (len(snap_db["runs"])+1)),
            time=time.strftime("%Y-%m-%d %H:%M:%S"),
            pid=TARGET_PID,
            ws=m0.get('ws',0), commit=m0.get('commit',0),
            census=census,
            chunks=[[r["O"]-0x40, r["tsize"],
                     sum(s for (_,s,_) in r.get("busy",[])),
                     len(r.get("busy",[]))]
                    for r in allc.values() if r["kind"]=='var' and r["tsize"]>=0x40000],
            bigalloc=[[b, sz] for (b, sz, _) in bigalloc],
            logstrings=[[p, ln, nm2] for (p, ln, nm2) in logstrings],
            frames=[[p, v] for (p, v) in frame_rows],
            tflists=[[t["obj"], t["arrsz"], t["cnt"]] for t in top[:10]],
            tflist_addrs=tflist_addrs,
            tflist_new=tflist_new,
            fmcomps=fmcomps,
            fmcomps_new=fmcomps_new,
            pinners=pinners,
        )
        if snap_db["runs"]:
            _pd = prev_entry if prev_entry is not None else snap_db["runs"][-1]
            if _pd.get("pid") != entry["pid"]:
                W("NOTE: previous run has a different pid - numbers are not directly comparable.")
            W("diff vs previous run (%s @ %s):" %
              (_pd.get("label"), _pd.get("time")))
            for line in pair_diff_lines(_pd, entry):
                W(line)
        else:
            W("first run - snapshot stored; run again after your next")
            W("compare->close cycle to get the unboundedness diff.")
        snap_save(snap_db, entry)
        W("snapshot #%d saved -> %s" % (len(snap_db["runs"]), snap_path()))
        if SAVE_PATH:
            try:
                with open(SAVE_PATH, "w", encoding="utf-8") as f:
                    json.dump(entry, f, indent=1)
                W("single-run snapshot also saved -> %s" % SAVE_PATH)
            except Exception as e:
                W("snapshot save failed: %r" % (e,))
        W("")

        # ---- phase F: trim + verdict ----
        W("--- phase F: trim ---")
        f=None; src=None
        try:
            f=psapi.EmptyWorkingSet; src="psapi.EmptyWorkingSet"
        except AttributeError:
            try:
                f=k32.K32EmptyWorkingSet; src="kernel32.K32EmptyWorkingSet"
            except AttributeError:
                f=None
        ews="n/a (no export found)"
        if f is not None:
            f.argtypes=[HP]; f.restype=ctypes.c_int
            ews = "%s=%s" % (src, "ok" if f(PROC) else "fail")
        r2 = k32.SetProcessWorkingSetSize(PROC, ctypes.c_size_t(-1), ctypes.c_size_t(-1))
        m1=meminfo()
        W("  %s ; SetProcessWorkingSetSize(-1,-1)=%s" % (ews, bool(r2)))
        W("  WS     : %.1f -> %.1f MB" % (m0.get('ws',0)/1e6, m1.get('ws',0)/1e6))
        W("  commit : %.1f -> %.1f MB" % (m0.get('commit',0)/1e6, m1.get('commit',0)/1e6))
        W("")

        W("--- verdict ---")
        if bigalloc:
            W("  fix ACTIVE: %d bigalloc mapping(s), %.1f MB outside the FPC heap." %
              (len(bigalloc), sum(sz for (_,sz,_) in bigalloc)/1e6))
        else:
            W("  fix NOT ACTIVE (no bigalloc magic regions) - build predates")
            W("  proc_bigalloc, or no big allocation is currently live.")
        pinned=[r for r in bigch
                if r.get("busy") and sum(s for (_,s,_) in r["busy"]) < 0.5*r["tsize"]]
        for r in pinned:
            bsum=sum(s for (_,s,_) in r["busy"])
            W("  pinned FPC chunk 0x%x (%.1f MB) held by %.3f MB - see phase C6" %
              (r["O"]-0x40, r["tsize"]/1e6, bsum/1e6))
        leaked=[v for (_,v) in frame_rows if v.startswith("LEAKED")]
        if leaked:
            W("  %d TEditorFrame object(s) LEAKED (not in any frame-list, no tab" % len(leaked))
            W("  control referrer) - the compare#2 subtree; check the app log for")
            W("  the new 'ERROR: cannot free closed editor frame' line.")
        else:
            W("  no leaked TEditorFrame found (%d live, all attached)." % len(frame_rows))
        grow = [c for c in ("TATSynEdit","TEditorFrame","TTimer","TATStrings")
                if census.get(c,0) > 12]
        if grow:
            W("  watch-list classes currently above baseline: %s" % ", ".join(grow))
        W("")
        W("compare#3 protocol: run v27 (this), compare big files, close the tab,")
        W("run 'python v27.py --label after1', compare again, close, run")
        W("'--label after2'. The diff lines tell you if anything still grows.")
        W("(offline re-diff anytime:  python v27.py --diff v27_hist.json)")
        W("(all pairs + trend table:  python v27.py --diffall v27_hist.json)")
        W("")
        W("v27 finished.")

        say("v27 finished -> " + OUT)
        say("pid=%d mode=%s | WS=%.1f commit=%.1f MB | bigalloc=%d (%.1f MB) | frames=%d (leaked=%d)" %
            (TARGET_PID, "in-proc" if INPROC else "external",
             m1.get('ws',0)/1e6, m1.get('commit',0)/1e6,
             len(bigalloc), sum(sz for (_,sz,_) in bigalloc)/1e6,
             len(frame_rows), len(leaked)))

    except Exception:
        import traceback
        W("FATAL in v27:")
        W(traceback.format_exc())
        say("v27 FAILED - see tail of " + OUT)
    finally:
        rep.close()

# ---------------- dispatch (runs after every def above is live) ----------------
if DIFFALL_FILES is not None:
    sys.exit(cmd_diffall(DIFFALL_FILES))

if DIFF_FILES is not None:
    sys.exit(cmd_diff(DIFF_FILES))

if not WIN:
    say("v27: scanning needs Windows (kernel32/psapi); --diff/--diffall work anywhere.")
    sys.exit(2)

if TARGET_PID is None:
    _c = [] if GENERIC else find_cudatext_pids()
    if _c:
        TARGET_PID = _c[0][0]
        say("v27: auto-found %s (pid %d)%s" %
            (_c[0][1], _c[0][0], "" if len(_c)==1 else "  [also running: %s]" %
             ", ".join("%s(%d)"%(n,p) for p,n in _c[1:])))
if TARGET_PID is None:
    if GENERIC:
        say("v27: generic mode needs an explicit pid:  python v27.py <pid> --generic")
    else:
        say("v27: no CudaText process found - pass the pid:  python v27.py <pid>")
    sys.exit(1)

PROC = None
for rights in (0x0510, 0x1F0FFF):
    h = k32.OpenProcess(rights, 0, TARGET_PID)
    if h: PROC = h; break
if PROC is None:
    say("v27: OpenProcess(%d) failed (err=%d) - run as the same user" %
        (TARGET_PID, ctypes.get_last_error()))
    sys.exit(1)
INPROC = (k32.GetCurrentProcessId() == TARGET_PID)
if INPROC:
    say("v27: WARNING - running INSIDE the target process (console exec?).")
    say("     Old-scan remnants will pollute; prefer an external cmd.exe run.")

snap_db = {"runs": []} if GENERIC else snap_load()
say("v27: scanning pid %d (%s)%s, report -> %s" %
    (TARGET_PID, "IN-PROCESS - expect phantoms!" if INPROC else "external",
     (" label=" + LABEL) if LABEL else "", OUT))
try:
    main()
except Exception:
    import traceback; traceback.print_exc()
    say("v27 hit an error - target process is not affected; paste the traceback")


