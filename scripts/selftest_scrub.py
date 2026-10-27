#!/usr/bin/env python3
"""Selftest for burn --scrub + scrub.py transcript scrubber. Sandbox HOME only; never touches real state.db."""
import os, sys, json, time, sqlite3, subprocess, tempfile, shutil

SKILL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "skills",
           "security", "incognito-blacksite", "scripts")
HOME = tempfile.mkdtemp(prefix="bs-scrub-test-")
os.environ["HOME"] = HOME
hermes = os.path.join(HOME, ".hermes"); os.makedirs(os.path.join(hermes, "logs"))
DB = os.path.join(hermes, "state.db"); LOG = os.path.join(hermes, "logs", "agent.log")

SESS = "20261026_testsession_aa11"
OTHER = "20261020_othersession_bb22"
SECRET = "operation seahawk target list"   # a "sensitive pattern"
BSID_MARK = "topsecret-filename.zip"

db = sqlite3.connect(DB)
db.executescript("""
CREATE TABLE sessions (id TEXT PRIMARY KEY, started_at REAL, context_summary TEXT, title TEXT, model TEXT);
CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, timestamp REAL, role TEXT,
  content TEXT, tool_calls TEXT, tool_name TEXT, reasoning TEXT, reasoning_content TEXT,
  api_content TEXT, active INTEGER DEFAULT 1, message_uid TEXT);
CREATE TABLE state_meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE delivery_obligations (obligation_id TEXT PRIMARY KEY, session_key TEXT, content TEXT);
CREATE VIEW messages_fts_src AS SELECT id, content, tool_name, tool_calls FROM messages;
CREATE VIRTUAL TABLE messages_fts USING fts5(content, tool_name, tool_calls, content='messages_fts_src', content_rowid='id');
CREATE TRIGGER messages_fts_insert AFTER INSERT ON messages
BEGIN
  INSERT INTO messages_fts(rowid, content, tool_name, tool_calls)
  VALUES (new.id, new.content, new.tool_name, new.tool_calls);
END;
CREATE TRIGGER messages_fts_update AFTER UPDATE OF content, tool_name, tool_calls ON messages
BEGIN
  INSERT INTO messages_fts(messages_fts, rowid, content, tool_name, tool_calls)
  VALUES ('delete', old.id, old.content, old.tool_name, old.tool_calls);
  INSERT INTO messages_fts(rowid, content, tool_name, tool_calls)
  VALUES (new.id, new.content, new.tool_name, new.tool_calls);
END;
""")
t = time.time()
db.execute("INSERT INTO sessions VALUES (?,?,?,?,?)", (SESS, t-7200, f"summary mentions {SECRET}", "cocktail neon green", "m"))
db.execute("INSERT INTO sessions VALUES (?,?,?,?,?)", (OTHER, t-72000, "unrelated summary", "other title", "m"))
rows = [
    (SESS, t-3600, "user", f"do {SECRET} with {BSID_MARK}", None, None, "thinking about "+SECRET, None, f'{{"cmd":"open {BSID_MARK}"}}'),
    (SESS, t-3500, "assistant", f"plan for {SECRET}", '[{"name":"terminal","args":{"cmd":"cat '+BSID_MARK+'"}}]', "terminal", None, None, None),
    (SESS, t-3400, "tool", f"contents of {BSID_MARK}: {SECRET}", None, "terminal", None, None, None),
    (SESS, t-300,  "user", "post-burn innocent chat", None, None, None, None, None),   # AFTER window (t1 covers +600s... will be scrubbed if in range; keep to prove RANGE bound with t1 override below)
    (OTHER, t-3500, "user", f"unrelated {SECRET} mention elsewhere", None, None, None, None, None),  # token pass must catch this
]
for r in rows:
    db.execute("INSERT INTO messages (session_id,timestamp,role,content,tool_calls,tool_name,reasoning,reasoning_content,api_content) VALUES (?,?,?,?,?,?,?,?,?)", r)
db.execute("INSERT INTO state_meta VALUES ('session_summary:%s', 'compressed: %s')" % (SESS, SECRET))
db.execute("INSERT INTO delivery_obligations VALUES ('o1', ?, ?)", (SESS, f"queued reply about {SECRET}"))
db.commit(); db.close()

open(LOG, "w").write("\n".join([
    f"2026-10-26 10:00:00 INFO [gateway] session={SESS} msg: {SECRET}",
    "2026-10-26 10:00:01 INFO [gateway] session=OTHER unrelated keep-me line",
    f"2026-10-26 10:00:02 INFO [agent] file {BSID_MARK} opened",
]) + "\n")

# ---- plan the "burn" would have staged; also test the full open→burn --scrub flow ----
incog = ["python3", os.path.join(SKILL, "incog.py")]
env = dict(os.environ)
out = subprocess.run(incog + ["open"], capture_output=True, text=True, env=env).stdout.split()
bsid = out[0]
r = subprocess.run(incog + ["burn", bsid, "--scrub", "--session", SESS, "--pattern", SECRET, "--pattern", BSID_MARK],
                   capture_output=True, text=True, env=env)
assert "plan staged" in r.stdout, r.stdout + r.stderr
planfile = os.path.join(HOME, ".incognito", ".scrub-" + __import__("hashlib").sha256(bsid.encode()).hexdigest()[:12] + ".json")
assert os.path.exists(planfile), "plan not staged"
plan = json.load(open(planfile)); plan["t0"] = t - 4000; plan["t1"] = t - 1000  # window brackets the 3 seeded rows
json.dump(plan, open(planfile, "w"))

r = subprocess.run(incog + ["scrub", bsid, "--once", "--interval", "1", "--clean-rounds", "1",
                            "--max-rounds", "1", "--report", os.path.join(HOME, "rep.json")],
                   capture_output=True, text=True, env=env, timeout=120)
print(r.stdout[-800:], r.stderr[-400:])

# ---- assertions ----
fails = []
def ck(name, cond): (fails.append(name) if not cond else None)
db = sqlite3.connect(DB)
q = lambda s, *a: db.execute(s, a).fetchall()
M = "[REDACTED]"
in_range = q("SELECT content FROM messages WHERE session_id=? AND timestamp BETWEEN ? AND ?", SESS, plan["t0"], plan["t1"])
ck("range rows markered", all(c == M for (c,) in in_range) and len(in_range) == 3)
post = q("SELECT content FROM messages WHERE session_id=? AND timestamp>?", SESS, plan["t1"])
ck("post-window content untouched", post and post[0][0] == "post-burn innocent chat")
ck("tool_calls scrubbed", not any(SECRET in (x or "") or BSID_MARK in (x or "") for (x,) in q("SELECT tool_calls FROM messages")))
ck("api_content scrubbed", not any(SECRET in (x or "") for (x,) in q("SELECT api_content FROM messages")))
ck("reasoning scrubbed", not any(SECRET in (x or "") for (x,) in q("SELECT reasoning FROM messages")))
ck("sessions summary keyed", q("SELECT context_summary FROM sessions WHERE id=?", SESS)[0][0] == M)
ck("other session summary intact", "unrelated summary" in q("SELECT context_summary FROM sessions WHERE id=?", OTHER)[0][0])
ck("token pass cross-session", not any(SECRET in (c or "") for (c,) in q("SELECT content FROM messages")))  # other session's mention replaced
ck("state_meta scrubbed", not any(SECRET in (v or "") for (v,) in q("SELECT value FROM state_meta")))
ck("delivery_obligations scrubbed", not any(SECRET in (c or "") for (c,) in q("SELECT content FROM delivery_obligations")))
ck("session_id structural col kept", len(q("SELECT id FROM messages WHERE session_id=?", SESS)) == 4)
logs = open(LOG).read()
ck("log secret masked", SECRET not in logs and BSID_MARK not in logs)
ck("log sid masked", SESS not in logs)
ck("log other-session intact", "unrelated keep-me line" in logs and "OTHER" in logs)
ck("log length preserved", logs.count("\n") == 3)
fts = q("SELECT COUNT(*) FROM messages_fts WHERE messages_fts MATCH ?", f'"{SECRET.split()[0]}*"')
ck("fts reindexed clean", fts[0][0] == 0)
ck("plan shredded", not os.path.exists(planfile))
rep = json.load(open(os.path.join(HOME, "rep.json")))
print("report:", json.dumps(rep))
ck("report counts only", SECRET not in open(os.path.join(HOME,"rep.json")).read() and SESS not in open(os.path.join(HOME,"rep.json")).read())
ck("byte probe clean", sum(rep["byte_probe"].values()) == 0)

shutil.rmtree(HOME, ignore_errors=True)
# ---- v1.7 defaults: burn stages scrub by default (env sid); --no-scrub opts out ----
env2 = dict(env); env2["HERMES_SESSION_ID"] = SESS
bsid2 = subprocess.run(incog + ["open"], capture_output=True, text=True, env=env2).stdout.split()[0]
subprocess.run(incog + ["burn", bsid2], capture_output=True, text=True, env=env2)
p2 = os.path.join(HOME, ".incognito", ".scrub-" + __import__("hashlib").sha256(bsid2.encode()).hexdigest()[:12] + ".json")
ck("default burn stages scrub plan via env sid", os.path.exists(p2))
if os.path.exists(p2):
    json.dump(json.load(open(p2)), open(os.path.join(HOME, "plan2-saved.json"), "w"))  # keep for inspection; sandbox burns nothing live
bsid3 = subprocess.run(incog + ["open"], capture_output=True, text=True, env=env2).stdout.split()[0]
subprocess.run(incog + ["burn", bsid3, "--no-scrub"], capture_output=True, text=True, env=env2)
p3 = os.path.join(HOME, ".incognito", ".scrub-" + __import__("hashlib").sha256(bsid3.encode()).hexdigest()[:12] + ".json")
ck("--no-scrub skips staging", not os.path.exists(p3))

print("\n".join("FAIL: " + f for f in fails) if fails else "ALL FIXTURE ASSERTIONS PASSED")
sys.exit(1 if fails else 0)
