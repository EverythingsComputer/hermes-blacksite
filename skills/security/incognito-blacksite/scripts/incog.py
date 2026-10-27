#!/usr/bin/env python3
"""Blacksite incognito-mode ledger + burner. Stdlib only.

  incog.py open [--ram]                -> create session dir, print "<id> <dir> [ram|disk]"
                                          --ram uses /run/user/$UID/incognito (tmpfs, RAM-backed,
                                          refuses to start if not tmpfs)
  incog.py log <id> create <path>... -> record creations (BEFORE doing them)
  incog.py log <id> modify <path>    -> backup existing file into session dir, record
  incog.py log <id> delete <path>    -> backup existing file/dir, record
  incog.py log <id> <other> <path>   -> record misc action (clone/download/etc), no backup
  incog.py keep <id> <path>          -> move file to outbox/ so the burn spares it
  incog.py list                      -> active session ids (those without BURNED marker)
  incog.py burn <id> [--dry] [--no-scrub] [--session <hermes-sid>] [--pattern <p>]...
                                      -> reverse ledger newest->oldest, shred session dir;
                                         transcript scrub is DEFAULT on every burn (plan
                                         staged in ~/.incognito/, run `scrub <id>` after);
                                         --no-scrub skips it; session id from
                                         HERMES_SESSION_ID env or --session
  incog.py scrub <id> [--interval N] -> exec scripts/scrub.py with the staged plan
"""
import sys, os, json, time, shutil, subprocess, hashlib

ROOT = os.path.expanduser("~/.incognito")
OUTBOX = os.path.join(ROOT, "outbox")

def ram_root():
    return f"/run/user/{os.getuid()}/incognito"

def is_tmpfs(path):
    r = subprocess.run(["findmnt", "-T", path, "-n", "-o", "FSTYPE"], capture_output=True, text=True)
    return r.stdout.strip() in ("tmpfs",)

def die(msg):
    sys.stderr.write("incog: " + msg + "\n"); sys.exit(1)

def sdir(sid):
    for base in (ROOT, ram_root()):
        d = os.path.join(base, sid)
        if os.path.isdir(d): return d
    die(f"no session {sid}")

def ledger_path(d):
    return os.path.join(d, "LEDGER.jsonl")

def read_ledger(d):
    """Merge main + .bak, dedupe, sort by ts. A torn line (crash mid-append) in one
    file is covered by the mirror in the other."""
    seen = {}
    for f in (ledger_path(d), ledger_path(d) + ".bak"):
        if not os.path.exists(f): continue
        with open(f, errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line: continue
                try: row = json.loads(line)
                except json.JSONDecodeError: continue
                if not isinstance(row, dict): continue
                key = (row.get("ts"), row.get("action"), row.get("target"))
                if key not in seen or row.get("backup"): seen[key] = row
    return sorted(seen.values(), key=lambda r: r.get("ts") or 0)

def append(d, row):
    for f in (ledger_path(d), ledger_path(d) + ".bak"):
        with open(f, "a") as fh:
            fh.write(json.dumps(row) + "\n")

def backup(d, path):
    if not os.path.exists(path): return None
    h = hashlib.sha256(path.encode()).hexdigest()[:12]
    name = os.path.basename(path.rstrip("/")) or "root"
    dest_dir = os.path.join(d, "backups", h)
    os.makedirs(dest_dir, exist_ok=True)
    # microsecond precision + collision loop: two logs in the SAME second must not clobber
    for attempt in range(100):
        dest = os.path.join(dest_dir, name + ".t" + str(int(time.time() * 1e6)))
        if not os.path.exists(dest): break
        time.sleep(0.001)
    try:
        if os.path.isdir(path):
            copytree_safe(path, dest)
        else:
            st = os.lstat(path)
            import stat as _stat
            if not _stat.S_ISREG(st.st_mode): return None
            if st.st_size > (2 << 30): die(f"backup too large ({path} > 2GiB); accept no-restore with a manual copy")
            shutil.copy2(path, dest)
    except (OSError, shutil.Error) as e:
        shutil.rmtree(dest, ignore_errors=True)
        die(f"backup FAILED for {path}: {e} — refusing to log an un-restorable action")
    return dest

def copytree_safe(src, dst, limit_files=20000, limit_bytes=2 << 30):
    """Regular files + symlinks only; skips sockets/FIFOs/devices; refuses runaway sizes."""
    import stat as _stat
    count = total = 0
    os.makedirs(dst, exist_ok=True)
    for root, dirs, files in os.walk(src):
        rel = os.path.relpath(root, src)
        base = dst if rel == "." else os.path.join(dst, rel)
        os.makedirs(base, exist_ok=True)
        for fn in files:
            sp = os.path.join(root, fn)
            try: st = os.lstat(sp)
            except OSError: continue
            dp = os.path.join(base, fn)
            if _stat.S_ISLNK(st.st_mode):
                try: os.symlink(os.readlink(sp), dp)
                except OSError: pass
                continue
            if not _stat.S_ISREG(st.st_mode): continue
            count += 1; total += st.st_size
            if count > limit_files or total > limit_bytes:
                shutil.rmtree(dst, ignore_errors=True)
                die(f"backup refused: {src} too large ({count} files, {total>>20} MiB copied) — snapshot it yourself if you really mean it")
            try: shutil.copy2(sp, dp)
            except OSError as e:
                shutil.rmtree(dst, ignore_errors=True)
                die(f"backup incomplete: cannot read {sp} ({e.strerror}) — fix perms or exclude it, nothing logged")

def shred(path):
    """Best-effort: shred -u files; plain rmtree for dirs; never follow into /, /home tops.
    NOTE: on tmpfs, shred just clears RAM pages (no disk media to overwrite) — fine."""
    try:
        if os.path.islink(path) or os.path.isfile(path):
            if shutil.which("shred"):
                subprocess.run(["shred", "-u", "-n", "1", "--" , path], timeout=120,
                               stderr=subprocess.DEVNULL)
            else:
                os.unlink(path)
        elif os.path.isdir(path):
            for root, dirs, files in os.walk(path):
                for fn in files: shred(os.path.join(root, fn))
            os.rmdir(path)
    except (FileNotFoundError, PermissionError, OSError, subprocess.TimeoutExpired):
        try: os.unlink(path)
        except OSError: pass

def in_root(path):
    ap = os.path.abspath(path)
    for base in (ROOT, ram_root()):
        ab = os.path.abspath(base)
        if ap == ab or ap.startswith(ab + os.sep): return True
    return False

HOME = os.path.expanduser("~")
PROTECTED_TOP = {"/", "/home", "/root", "/usr", "/etc", "/var", "/bin", "/sbin", "/lib", "/opt",
                 "/boot", "/sys", "/proc", "/dev", "/run", "/tmp", "/var/tmp", os.path.dirname(HOME), HOME,
                 os.path.dirname(ram_root()), f"/run/user/{os.getuid()}"}

def protected(t):
    """Refuse reverse-delete of anything too broad to undo."""
    ap = os.path.abspath(t)
    if ap in PROTECTED_TOP: return True
    if len([s for s in ap.split(os.sep) if s]) <= 1: return True   # any top-level dir
    if ap == os.path.abspath(os.sep): return True
    return False

# ---------- commands ----------
def cmd_open(args=None):
    args = args or []
    ram = "--ram" in args
    root = ram_root() if ram else ROOT
    if ram:
        parent = os.path.dirname(root)
        if not os.path.isdir(parent):
            die(f"runtime dir {parent} missing; no per-user tmpfs available")
        try:
            os.makedirs(root, mode=0o700, exist_ok=True)
        except (FileExistsError, NotADirectoryError, OSError) as e:
            die(f"cannot create {root}: {e} — remove or fix the blocking path")
        if not is_tmpfs(root):
            die(f"{root} is NOT tmpfs — refusing (--ram guarantees RAM-only)")
    else:
        try:
            os.makedirs(root, mode=0o700, exist_ok=True)
        except (FileExistsError, NotADirectoryError, OSError) as e:
            die(f"cannot create {root}: {e} — remove or fix the blocking path")
    sid = time.strftime("bs%Y%m%d-%H%M%S") + "-" + os.urandom(2).hex()
    d = os.path.join(root, sid)
    os.makedirs(os.path.join(d, "backups"), mode=0o700)
    append(d, {"ts": time.time(), "action": "session_open", "target": d, "ram": ram})
    print(sid, d, "[ram]" if ram else "[disk]")

def cmd_log(args):
    sid, action = args[0], args[1]
    KNOWN = {"create", "modify", "delete", "download", "clone", "extract", "net", "other"}
    if action not in KNOWN:
        die(f"unknown action '{action}' — a typo silently disables reversal; use one of {sorted(KNOWN)}")
    targets = args[2:] or [""]
    d = sdir(sid)
    for t in targets:
        t = t.strip()
        if not t:
            die(f"refusing to log {action} with empty target (cwd-resolution hazard)")
        ap = os.path.abspath(t)
        if protected(ap):
            die(f"refusing to log {action} on protected path: {ap}")
        act = action
        row = {"ts": time.time(), "action": act, "target": ap}
        if act in ("modify", "delete"):
            if not os.path.exists(ap):
                die(f"{act} target does not exist: {ap} (use 'create' for new paths)")
            row["backup"] = backup(d, ap)
        elif act == "create" and os.path.lexists(ap):
            # MIS-LOGGED CREATE: path already exists -> it is a modify in disguise.
            # Back it up now; burn will restore instead of deleting.
            row["action"] = act = "create_over_existing"
            row["backup"] = backup(d, ap)
            sys.stderr.write(f"incog: NOTE {ap} already exists; logged as create_over_existing (will be restored, not deleted)\n")
        append(d, row)
        print(f"logged {act} {ap}" + (" (backed up)" if row.get("backup") else ""))

def cmd_keep(args):
    sid, path = args[0], os.path.abspath(args[1])
    d = sdir(sid)
    outbox = os.path.join(os.path.dirname(d), "outbox")  # same medium as session (ram or disk)
    if not in_root(path):
        die("keep only works on files inside the session dir; stage copies there first")
    # scope: YOUR session dir only — never another session's or the shared outbox
    real_d, real_p = os.path.realpath(d), os.path.realpath(path)
    try:
        inside = os.path.commonpath([real_p, real_d]) == real_d
    except ValueError:
        inside = False
    if not inside or real_p == real_d:
        die(f"keep: {path} does not belong to session {sid}")
    os.makedirs(outbox, mode=0o700, exist_ok=True)
    dest = os.path.join(outbox, sid + "__" + os.path.basename(path))
    n = 1
    while os.path.exists(dest):
        dest = os.path.join(outbox, f"{sid}__{os.path.basename(path)}.{n}"); n += 1
    try:
        shutil.move(path, dest)
    except OSError as e:
        die(f"keep failed: {e} (outbox {outbox} writable?)")
    append(d, {"ts": time.time(), "action": "keep", "target": path, "moved_to": dest})
    print("spared from burn:", dest)

def cmd_list(args=None):
    found = False
    for base in (ROOT, ram_root()):
        if not os.path.isdir(base): continue
        for sid in sorted(os.listdir(base)):
            d = os.path.join(base, sid)
            if os.path.isdir(d) and sid.startswith("bs") and not os.path.exists(os.path.join(d, "BURNED")):
                tag = " [ram]" if base == ram_root() else ""
                print(sid + tag); found = True
    if not found: print("(none)")

def reverse(row, dry):
    a, t = row["action"], row.get("target", "")
    def note(desc):
        print(("DRY " if dry else "") + desc)
    if a == "create":
        if not t or in_root(t): return
        if protected(t):
            note(f"REFUSE reverse-delete protected path {t} — leaving in place")
            return
        if os.path.lexists(t):
            if os.path.isdir(t) and os.listdir(t):
                note(f"SKIP dir {t} (not empty at burn — unknown extra content; remove manually)")
                return
            note(f"rm created {t}")
            if not dry: shred(t)
    elif a == "create_over_existing":
        b = row.get("backup")
        if b and os.path.exists(b):
            note(f"restore pre-existing {t} <- backup (was mis-logged as create)")
            if not dry:
                try:
                    if os.path.isdir(b): shutil.rmtree(t, ignore_errors=True); shutil.copytree(b, t, symlinks=True)
                    else: shutil.copy2(b, t)
                except OSError as e: print(f"  FAILED restore {t}: {e}")
    elif a == "modify":
        b = row.get("backup")
        if b and os.path.exists(b) and os.path.isabs(t):
            note(f"restore {t} <- backup")
            if not dry:
                try:
                    if os.path.isdir(b): shutil.rmtree(t, ignore_errors=True); shutil.copytree(b, t, symlinks=True)
                    else: shutil.copy2(b, t)
                except OSError as e: print(f"  FAILED restore {t}: {e}")
        else: note(f"SKIP restore {t} (no backup)")
    elif a == "delete":
        b = row.get("backup")
        if b and os.path.exists(b):
            note(f"undelete {t} <- backup")
            if not dry:
                try:
                    if os.path.isdir(b): shutil.copytree(b, t, symlinks=True)
                    else: shutil.copy2(b, t)
                except OSError as e: print(f"  FAILED undelete {t}: {e}")
    # other actions: informational only

def plan_path(base, bsid):
    return os.path.join(base, ".scrub-" + hashlib.sha256(bsid.encode()).hexdigest()[:12] + ".json")

def cmd_burn(args):
    sid = args[0]; dry = "--dry" in args[1:]
    scrub = "--no-scrub" not in args[1:]   # default ON: every burn cleans the transcript
    hermes_sid = None; patterns = []
    rest = args[1:]
    for i, x in enumerate(rest):
        if x == "--session" and i + 1 < len(rest): hermes_sid = rest[i + 1]
        if x == "--pattern" and i + 1 < len(rest): patterns.append(rest[i + 1])
    if scrub and not hermes_sid:
        hermes_sid = os.environ.get("HERMES_SESSION_ID")  # argv gets logged; env doesn't
    d = sdir(sid)
    if scrub and not dry and not hermes_sid:
        print("warning: no session id (env HERMES_SESSION_ID or --session) — transcript scrub skipped")
        scrub = False
    rows = read_ledger(d)
    t0 = rows[0]["ts"] if rows else time.time()
    if scrub and not dry:
        # plan lives OUTSIDE the session dir (it must survive the shred) but holds the
        # target strings, so scrub.py shreds it when done. Patterns go in it, never in argv.
        p = plan_path(os.path.dirname(d), sid)
        with open(p, "w") as fh:
            json.dump({"bsid": sid, "session_id": hermes_sid, "t0": t0, "t1": time.time() + 600,
                       "patterns": patterns, "db": os.path.expanduser("~/.hermes/state.db"),
                       "logs": os.path.expanduser("~/.hermes/logs"),
                       "marker": "[REDACTED]"}, fh)
        os.chmod(p, 0o600)
    n_rev = n_skip = 0
    for row in reversed(rows):
        if row["action"] in ("create", "modify", "delete", "create_over_existing"):
            reverse(row, dry); n_rev += 1
        else:
            n_skip += 1
    if not dry:
        # shred everything in the session dir except spared outbox (lives outside)
        for entry in os.listdir(d):
            shred(os.path.join(d, entry))
        try: os.rmdir(d)
        except OSError: shutil.rmtree(d, ignore_errors=True)
    print(f"{'DRY ' if dry else ''}burn {sid}: {'would reverse' if dry else 'reversed'} {n_rev} actions, {n_skip} noted-only.")
    if not dry and os.path.isdir(d):
        # dir may remain if ledger lived... it's inside and shredded; ensure gone
        shutil.rmtree(d, ignore_errors=True)
    if not dry and os.path.exists(d):
        die("session dir survived burn — inspect manually")
    if scrub and not dry:
        print(f"transcript scrub plan staged — run:  python3 {os.path.abspath(__file__)} scrub {sid}")

def cmd_scrub(args):
    sid = args[0]
    for base in (ROOT, ram_root()):
        p = plan_path(base, sid)
        if os.path.exists(p):
            exe = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scrub.py")
            os.execv(sys.executable, [sys.executable, exe, "--plan", p] + args[1:])
        # plan for ram sessions lives next to the session dir
    die(f"no scrub plan for {sid} (burn --scrub first; plans live in ~/.incognito/)")

def main():
    if len(sys.argv) < 2: die(__doc__ or "usage")
    cmd, args = sys.argv[1], sys.argv[2:]
    {"open": cmd_open, "log": cmd_log, "keep": cmd_keep, "list": cmd_list, "burn": cmd_burn,
     "scrub": cmd_scrub}.get(cmd, lambda a: die("unknown cmd " + cmd))(args)

if __name__ == "__main__":
    main()
