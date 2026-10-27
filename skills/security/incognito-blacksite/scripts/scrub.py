#!/usr/bin/env python3
"""Blacksite transcript scrubber — stdlib only. Generic: embeds NO sensitive strings.

  scrub.py --plan <file> [--interval 8] [--clean-rounds 6] [--max-rounds 150] [--once] [--report <path>]

The plan (written by `incog.py burn <bsid> --scrub --session <hermes-sid>`) is the ONLY
place the target strings live:
  {"bsid","session_id","t0","t1","patterns":[...],"db","logs","marker"}

Design rules baked in (all learned from the first live scrub — see SKILL.md):
  * NEVER hardcode a column list: every user table is enumerated via PRAGMA table_info
    and every TEXT column handled — content survives in tool_calls, api_content,
    reasoning variants, state_meta.value, sessions summaries, delegation JSONs...
  * Two passes per round:
      1. RANGE pass — messages rows for the session inside [t0,t1]: content-ish TEXT
         columns become the marker (structural cols kept so the transcript stays
         parseable). sessions-row summary/title scrubbed by key.
      2. TOKEN pass — pattern/token replacement via UPDATE..replace() on EVERY text
         column of EVERY table, scanning with instr() (exact substring). Covers
         rows the range pass can't see (later turns quoting the session id, etc.).
    FTS indexes stay in sync via the DB's own UPDATE triggers; verified by probe.
  * No db snapshots. No DELETE of session rows (guarded + destroys the live session).
  * Logs: in-place same-inode byte replacement (safe under live appenders), token
    occurrences masked with '#' of equal length; interleaved other-session lines
    untouched.
  * Watchdog: repeat until N consecutive fully-clean rounds (the scrub's own tool
    calls arrive AFTER a pass — they get caught by a later round), then ONE VACUUM
    attempt (fresh conn + busy_timeout; the between-turns window is what works),
    final byte probe of db+wal, re-scrub logs, write counts-only report, shred
    plan, remove own pidfile. Report never contains patterns or matched text.
"""
import sys, os, json, time, sqlite3, glob, hashlib, argparse, subprocess

def log(msg):
    print(f"[scrub {time.strftime('%H:%M:%S')}] {msg}", flush=True)

# ---------- db passes ----------
KEEP_COLS = {  # structural columns kept even inside the range (ids/enums carry no content)
    "session_id","role","tool_call_id","tool_call_uid","tool_call_uids","message_uid",
    "platform_message_id","absorbed_message_uids","finish_reason","display_kind",
    "effect_disposition","tool_name","id","timestamp","active","compacted",
}
SESSION_KEEP = {  # sessions columns scrubbed by key when they exist
    "context_summary","title","handoff_error","compression_failure_error",
    "last_activity_description","last_activity_provenance",
}

def tokens(plan, for_logs=False):
    """DB tokens: content patterns + the blacksite id — NEVER the hermes session id
    (it is a structural key in id/session_id columns; scrubbing it would break
    relations and stall the watchdog probing its own columns). Log tokens may
    include it: log lines have no schema."""
    base = [plan["bsid"]] + plan.get("patterns", [])
    if for_logs and plan.get("session_id"): base.append(plan["session_id"])
    return [t for t in dict.fromkeys(base) if t]

# id/structural columns the token pass must not rewrite (relations stay parseable)
STRUCT = KEEP_COLS | {"key","session_key","obligation_id","admission_id","request_id",
    "delegation_id","target_session_id","origin_session","origin_session_id","parent_session_id",
    "origin_ui_session_id","conversation_id","principal_id","owner_execution_id","execution_id",
    "payload_digest","adoption_digest","hash","instance_id","holder","scope","scope_key","room_id",
    "member_id","event_id","discussion_event_id","task_id","run_id","home_install_id",
    "target_install_id","authority_gateway_id","grant","cancellation_scope_id","trace_id",
    "profile","backend_id","host","source","created_source","user_id","chat_id","thread_id",
    "id","k","v","model","billing_provider","billing_base_url","billing_mode","cost_status",
    "cost_source","pricing_version","title_source","handoff_platform","profile_name",
    "transport_profile","tool_names","intent","status","outcome","state","delivery_state",
    "kind","actor_json","segscore","term","pgno","segid"}

def user_tables(conn):
    return [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]

def text_cols(conn, t):
    out = []
    for _, name, typ, *_ in conn.execute(f"PRAGMA table_info('{t}')"):
        u = (typ or "").upper()
        if u in ("", "TEXT", "CLOB") or u.startswith("TEXT"):
            out.append(name)
    return out

def range_pass(conn, plan):
    """messages inside [t0,t1] for the session -> marker; sessions row -> keyed scrub."""
    changed = 0
    tables = set(user_tables(conn))
    if "messages" in tables:
        cols = [c for c in text_cols(conn, "messages") if c not in KEEP_COLS]
        if cols:
            sets = ",".join(f'"{c}"=CASE WHEN "{c}" IS NOT NULL THEN ? ELSE NULL END' for c in cols)
            cur = conn.execute(
                f'UPDATE messages SET {sets} WHERE session_id=? AND timestamp>=? AND timestamp<=?',
                [plan["marker"]] * len(cols) + [plan["session_id"], plan["t0"], plan["t1"]])
            changed += cur.rowcount
    if "sessions" in tables:
        scols = [c for c in SESSION_KEEP if c in set(text_cols(conn, "sessions"))]
        if scols:
            sets = ",".join(f'"{c}"=CASE WHEN "{c}" IS NOT NULL THEN ? ELSE NULL END' for c in scols)
            cur = conn.execute(f'UPDATE sessions SET {sets} WHERE id=?', [plan["marker"]] * len(scols) + [plan["session_id"]])
            changed += cur.rowcount
    conn.commit()
    return changed

def token_pass(conn, plan):
    """token replacement across EVERY content TEXT column of EVERY user table (exact substring)."""
    toks = tokens(plan)
    changed = 0
    m = plan["marker"]
    for t in user_tables(conn):
        if t.endswith(("_data", "_idx", "_docsize", "_config")):  # fts shadow tables: read-only
            continue
        for c in text_cols(conn, t):
            if c in STRUCT:
                continue
            for tok in toks:
                try:
                    cur = conn.execute(
                        f'UPDATE "{t}" SET "{c}"=replace("{c}",?,?) '
                        f'WHERE "{c}" IS NOT NULL AND instr("{c}",?)>0',
                        (tok, m, tok))
                    changed += max(cur.rowcount, 0)
                except sqlite3.OperationalError:
                    pass  # fts virtual tables don't take UPDATE; their triggers fire via base table
    conn.commit()
    return changed

# ---------- probes ----------
def probe_db(conn, plan):
    hits = 0
    for t in user_tables(conn):
        if t.endswith(("_data", "_idx", "_docsize", "_config")): continue
        for c in text_cols(conn, t):
            if c in STRUCT: continue   # mirror token_pass or the watchdog stalls forever
            for tok in tokens(plan):
                try:
                    hits += conn.execute(f'SELECT COUNT(*) FROM "{t}" WHERE "{c}" IS NOT NULL '
                                         f'AND instr("{c}",?)>0', (tok,)).fetchone()[0]
                except sqlite3.OperationalError:
                    pass
    # range residue: any in-range message that still has non-marker text?
    try:
        cols = [c for c in text_cols(conn, "messages") if c not in KEEP_COLS]
        if cols:
            where = " OR ".join(f'"{c}" IS NOT NULL AND "{c}"<>?' for c in cols)
            hits += conn.execute(
                f'SELECT COUNT(*) FROM messages WHERE session_id=? AND timestamp>=? AND timestamp<=? AND ({where})',
                [plan["session_id"], plan["t0"], plan["t1"]] + [plan["marker"]] * len(cols)).fetchone()[0]
    except sqlite3.OperationalError:
        pass
    return hits

def fts_rebuild(conn):
    """FTS triggers are high-water-gated in the real schema — updates to OLD rows
    can silently skip reindexing, leaving stale terms MATCH-able after scrubbing.
    'rebuild' re-syncs the whole index from the (already markerised) content."""
    for f in ("messages_fts", "messages_fts_trigram"):
        try:
            conn.execute(f'INSERT INTO "{f}"("{f}") VALUES(\'rebuild\')')
        except sqlite3.OperationalError:
            pass
    conn.commit()

def probe_fts(conn, plan):
    """FTS should no longer match any pattern token (triggers reindex on our UPDATEs).
    Rows already holding a marker are excluded — FTS matching is case-insensitive and
    would otherwise self-match markers that contain a pattern word, stalling forever."""
    hits = 0
    for fts in ("messages_fts", "messages_fts_trigram"):
        try:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if fts not in tables: continue
            for p in plan.get("patterns", [])[:6]:
                word = "".join(ch for ch in p if ch.isalnum())
                if len(word) >= 4:
                    hits += conn.execute(
                        f'SELECT COUNT(*) FROM "{fts}" JOIN messages ON messages.id="{fts}".rowid '
                        f'WHERE "{fts}" MATCH ? AND COALESCE(messages.content,"") NOT LIKE \'%REDACTED%\'',
                        (f'"{word} *"',)).fetchone()[0]
        except sqlite3.OperationalError:
            pass
    return hits

def scrub_logs(logdir, plan):
    """in-place same-inode masking; returns lines blanked across all files."""
    toks = tokens(plan, for_logs=True)
    n = 0
    files = sorted(glob.glob(os.path.join(logdir, "*.log*"))) if logdir else []
    for f in files:
        try:
            with open(f, "r+b") as fh:
                data = fh.read()
                try: txt = data.decode("utf-8", "replace")
                except Exception: continue
                out = txt
                for tok in toks:
                    if tok and tok in out:
                        out = out.replace(tok, "#" * len(tok))
                if out != txt:
                    n += sum(1 for a, b in zip(txt.splitlines(), out.splitlines()) if a != b)
                    fh.seek(0); fh.write(out.encode()); fh.truncate(); fh.flush(); os.fsync(fh.fileno())
        except (PermissionError, OSError):
            continue
    return n

def probe_logs(logdir, plan):
    toks = tokens(plan, for_logs=True)
    hits = 0
    for f in sorted(glob.glob(os.path.join(logdir, "*.log*"))) if logdir else []:
        try:
            data = open(f, "rb").read()
            for tok in toks:
                hits += data.count(tok.encode())
        except OSError:
            pass
    return hits

def byte_probe(plan):
    """raw-byte scan for tokens in the live db file + wal (free pages included)."""
    hits = {}
    for path in (plan["db"], plan["db"] + "-wal"):
        if not os.path.exists(path): continue
        data = open(path, "rb").read()
        hits[os.path.basename(path)] = sum(data.count(t.encode()) for t in tokens(plan))  # no sid: legitimately structural in the db
    return hits

# ---------- main loop ----------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", required=True)
    ap.add_argument("--interval", type=float, default=8)
    ap.add_argument("--clean-rounds", type=int, default=6)
    ap.add_argument("--max-rounds", type=int, default=150)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--report")
    a = ap.parse_args()

    plan = json.load(open(a.plan))
    for k in ("session_id", "t0", "t1", "db", "marker"):
        if not plan.get(k): sys.exit(f"plan missing '{k}'")
    plan.setdefault("patterns", [])
    if not os.path.exists(plan["db"]): sys.exit(f"db not found: {plan['db']}")
    marker = plan["marker"]
    rep_path = a.report or os.path.join(os.path.dirname(a.plan),
                                        f"scrub-report-{hashlib.sha256(plan.get('bsid','x').encode()).hexdigest()[:8]}.json")
    rng_total = tok_total = lines_total = 0
    rounds = clean = 0
    while True:
        conn = sqlite3.connect(plan["db"], timeout=60000, isolation_level=None)
        try:
            conn.execute("PRAGMA busy_timeout=60000")
            conn.execute("BEGIN IMMEDIATE")
            r = range_pass(conn, plan)          # idempotent: marker<>marker ends repeats
            t = token_pass(conn, plan)
            conn.commit()
            try: conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.OperationalError: pass   # outside the txn, where it actually applies
        finally:
            conn.close()
        rng_total += max(r, 0); tok_total += max(t, 0)
        lines_total += scrub_logs(plan.get("logs"), plan)
        rounds += 1
        conn = sqlite3.connect(plan["db"], timeout=60000)
        try:
            db_hits = probe_db(conn, plan); fts_hits = probe_fts(conn, plan)
        finally:
            conn.close()
        log_hits = probe_logs(plan.get("logs"), plan)
        total = db_hits + fts_hits + log_hits
        clean = clean + 1 if total == 0 else 0
        log(f"round {rounds}: db_rows_changed={max(r,0)} tok_repl={max(t,0)} log_lines={lines_total} residue(db={db_hits},fts={fts_hits},logs={log_hits}) clean_streak={clean}")
        if a.once or (clean >= a.clean_rounds): break
        if rounds >= a.max_rounds:
            log(f"STALLED at residue={total} after {rounds} rounds — investigate per-target (db vs logs vs wal), not user chatter")
            break
        time.sleep(a.interval)

    vacuum_ok = False
    try:
        v = sqlite3.connect(plan["db"], timeout=60000, isolation_level=None)
        v.execute("PRAGMA busy_timeout=60000"); v.execute("VACUUM"); v.close()
        vacuum_ok = True; log("VACUUM done")
    except sqlite3.OperationalError as e:
        log(f"VACUUM refused ({e}); free-page copies self-clear as pages reuse")
    final = scrub_logs(plan.get("logs"), plan)
    bp = byte_probe(plan)
    conn = sqlite3.connect(plan["db"], timeout=60000)
    try:
        conn.execute("PRAGMA busy_timeout=60000")
        fts_hits_after = 0
        if probe_fts(conn, plan) > 0:
            fts_rebuild(conn)
            fts_hits_after = probe_fts(conn, plan)
        residue = probe_db(conn, plan) + fts_hits_after
    finally:
        conn.close()
    report = {"ts": time.time(), "bsid_digest": hashlib.sha256(plan.get("bsid", "").encode()).hexdigest()[:12],
              "range_rows": rng_total, "token_replacements": tok_total, "log_lines_masked": lines_total + final,
              "clean_streak": clean, "vacuum": vacuum_ok, "db_probe_residue": residue,
              "byte_probe": bp, "rounds": rounds}
    with open(rep_path, "w") as fh:
        json.dump(report, fh, indent=1)
        fh.flush(); os.fsync(fh.fileno())
    try:
        if subprocess.run(["shred", "-u", "--", a.plan], capture_output=True).returncode != 0:
            os.remove(a.plan)
    except Exception:
        try: os.remove(a.plan)
        except OSError: pass
    log(f"DONE report={rep_path} (counts only, no content)")

if __name__ == "__main__":
    main()
