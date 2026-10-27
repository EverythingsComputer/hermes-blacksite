---
name: incognito-blacksite
description: Says 'blacksite' to enter incognito; 'burnsite' to end.
version: 1.7.0
author: EverythingsComputer (Hermes Agent)
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [privacy, incognito, cleanup, security]
---

# Incognito / Blacksite Mode

## When to Use
User says the keyword `blacksite` (alone or in a sentence) to start a low-footprint working window, or `burnsite` to end and destroy it. Also use when the user asks for "incognito mode", "don't remember this", or "do X and leave no trace".

Triggers: user message contains the keyword **blacksite** (enter) or **burnsite** (exit+destroy). They are deliberate non-words — treat any use as intentional, even mid-sentence ("blacksite this for me", "burnsite when done"). State activation plainly at the top of the first reply: `◼ BLACKSITE ACTIVE — nothing persists.` and on exit `◼ BLACKSITE BURNED — <summary>`.

## Hard rules while active (until burnsite)
- **DESTRUCTION PRESUMPTION.** Everything created or changed in the window is intended to burn or be undone — this is the default for every artifact, no matter what was said about it. "Save it", "put it on my Desktop", "keep a copy", "send me the file" are STAGING instructions: do the work in the session dir, and the artifact burns. Only the user's own explicit extraction words — "keep this", "this one survives", "extract it", "yes, put that on my Desktop after the burn" — license `incog.py keep`. If it's not clear they mean it to outlive the burn: ask BEFORE any `keep`. Anticipatory keeps "to be helpful" are a violation; a wrong `keep` is undone immediately by moving the file back into the session dir.
- ZERO durable writes. Forbidden: memory tool (any target), skill_manage (any op incl. write_file), cronjob create/edit, git commit/push, edits under ~/Documents / ~/Downloads / ~/.config / ~/.hermes (except the session dir itself), package installs, config changes, crontab, autostart entries, systemd units, renaming/moving existing files outside the session dir.
- All scratch work goes in the session dir: `~/.incognito/<id>/` (created by scripts/incog.py open) — files, downloads, clones, extracts, generated artifacts. If the user wants an output "kept", it is still only staged there and MOVED OUT only after burnsite if they explicitly ask (that handoff survives the burn). Per the destruction presumption, "save it to X" wording during the window is NOT that explicit ask — only extraction words are.
- Terminal: keep commands self-contained; pass `HOME`-relative paths into the session dir; never `sudo` config mutations. Reads anywhere are fine.
- Subagents (delegate_task): start every `context` with "BLACKSITE MODE: no memory/skill/cron writes, no edits outside <session dir>; put all outputs there." Browser: use a dedicated named session; do NOT log into accounts; on burn close it and note that cookies/cache in that profile are not cryptographically wiped.
- If the user's request is impossible without a durable write (e.g. "install a package system-wide"), say so and ask — default is decline + find a session-dir-local alternative (pip --target, AppImage in session dir, etc.).

## Procedure — open
1. `python3 <skill>/scripts/incog.py open` → prints session dir + id. Remember the id (echo it in your reply).
   Add `--ram` (only when the user asks for RAM-only / extra ephemerality) to place the session in `/run/user/$UID/incognito/` — tmpfs, RAM-backed, never written to disk intentionally; the script REFUSES to open if the path isn't tmpfs. Caveats to state once when using it: (a) tmpfs pages CAN be swapped — if the user wants a real guarantee, `swapoff -a` needs sudo and is their call; note it, don't do it silently; (b) content vanishes on reboot/logout anyway; (c) `keep` outbox follows the same medium (ram outbox stays in RAM).
2. From now on every mutating action gets a ledger line (the script does it):
   - created file/dir: `incog.py log <id> create <path>`
   - modifying an EXISTING file outside the session dir (only allowed with explicit user opt-in): back up first — `incog.py log <id> modify <path>` copies a timestamped backup into the session dir before the edit.
   - deletions, downloads, clones, extraction: `incog.py log <id> <action> <path...>`
   Ledger = `~/.incognito/<id>/LEDGER.jsonl`. Log BEFORE doing the action (a failed reverse needs the record).

## Procedure — burnsite
1. `python3 <skill>/scripts/incog.py burn <id> --dry` → shows the reverse plan; sanity-check paths (never reverse-delete anything the ledger didn't create; script already refuses outside-reach paths and pre-existing parents).
2. `python3 <skill>/scripts/incog.py burn <id>` (actual): walks the ledger newest→oldest restoring backups / removing created files, `shred -u`s every file in the session dir, removes the dir. Reports reversed / skipped(needs-missing-backup) / unreverseable counts.
3. Extraction happens at the user's explicit request any time pre-burn — never agent-initiated, and never inferred from how the task was phrased (destruction presumption). Only files the user's own words put there survive: `incog.py keep <id> <path>` moves them to ~/.incognito/outbox/ untouched by the burn, then hand them off to the requested location AFTER the burn.
4. Report `◼ BLACKSITE BURNED` + the residual list (things that cannot be undone):
   - the conversation transcript itself (Hermes session history + Telegram server copy) — offer the **transcript scrub** (next section) if the user wants the local record burned too.
   - GPU/driver caches, ~/.cache entries from tools run during the window (note sizes, don't chase).
   - any outbound effect (email sent, request POSTed) — inherently un-reversible; state it.

## Procedure — transcript scrub (built-in, DEFAULT on every burn)
A burn destroys artifacts; the window's TEXT survives in the agent's own records (session DB + logs). Now a tool, not a hand-built watchdog:
1. At burn, scrub fires automatically unless the user explicitly said to keep the transcript (`--no-scrub`): `incog.py burn <id> [--pattern <p>]...` — reads the window's Hermes session id from `HERMES_SESSION_ID` (env, never argv) and stages a plan (0600) in `~/.incognito/` holding the target strings; extra known-sensitive phrases go via `--pattern` (never in argv beyond that; patterns live ONLY in the plan file). `--session <sid>` overrides the env id.
2. Run it: `python3 <skill>/scripts/incog.py scrub <id>` — launch via `terminal(background=true, persist_on_release=true)`. Defaults loop until 6 consecutive clean rounds, then ONE VACUUM, final byte-probe of db+wal, log re-scrub, counts-only report, and it shreds its own plan. Use `--clean-rounds 3` for a very quiet box.
3. Report the report path + counts. What it handles for you (do NOT hand-roll): full-table/full-column text sweeps (content lives in tool_calls, api_content, reasoning variants, state_meta, delegation JSONs — never hardcode a column list), FTS re-sync (`rebuild` — the real triggers are high-water-gated and skip OLD rows, so stale terms stay MATCH-able without it), structural-ID preservation (session_id/key columns stay intact — scrubbing them breaks relations AND stalls the watchdog probing its own columns), in-place same-inode log masking (safe under live appenders; other-session lines untouched), WAL checkpoint outside transactions, VACUUM via a fresh connection with busy_timeout (the between-turns idle window is what works; if refused, free-page copies self-clear as pages reuse).
4. Residuals it CANNOT reach (state plainly): messenger server copies (user deletes in-app), provider-side API request logs, content written AFTER t1 (the live tail — the scrub command's own rows are caught only via pattern tokens, not the range marker pass), and swap (tmpfs pages caveat).
If the loop STALLS (never reaches its clean streak) = sweep bug, not user chatter — the per-round residue breakdown (db/fts/logs) localizes it; old WAL pages and missed columns are the usual culprits.

## Pitfalls

- The "save it to X" trap: task phrasing that sounds like a durable save is still inside the window. First live use caught this — "make a game and save it on my Desktop" led to an anticipatory `keep`; under the destruction presumption the artifact was meant to burn, with extraction only if the user then said so in keep-words. Default: it burns.
- FTS5 external-content tables reject ordinary DML: direct INSERTs into the index corrupt it (`database disk image is malformed`), and a `replace` command raises an opaque `SQL logic error` — populate via an INSERT trigger and sync with the `rebuild` command. Related: UPDATE triggers gated on a high-water mark silently skip OLD rows, so stale terms stay MATCH-able until `rebuild` runs.
- Changes to scrub.py get validated ONLY through the fixture (release repo `scripts/selftest_scrub.py`, wired into selftest.sh; runs under a sandbox HOME). Never test against the live state.db — a scrub bug there is un-backout-able.
- TESTED ON LINUX ONLY (Ubuntu 26.04: tmpfs, coreutils shred, POSIX paths). Do not enable on Windows/macOS and claim ephemerality — shred/tmpfs/run-user assumptions break there; porting needs different primitives and a full re-test before any assurance is given.
- One burn per session: if two agents might touch the same box, agree on the session id first — `burn` mid-`log` from another agent fails safely ("no session") but the other agent's subsequent actions would then be UNLEDGERED. `list` shows active sessions to check.
- Safety rails are in the SCRIPT, not just the prose: unknown/typo actions refused at log (a typo silently disables reversal); empty targets die; protected roots (/, /home, ~, /tmp, /var, top-level dirs, /run/user/N) are refused at LOG time for every action; a `create` logged on a path that already exists is auto-demoted to `create_over_existing` (backed up now, RESTORED at burn, never deleted); reverse-delete of a directory that isn't empty at burn time is skipped with a warning; dir backups skip sockets/FIFOs/devices, preserve symlinks, and refuse >20k-file/>2GiB trees; a FAILED backup means NOTHING is logged (no un-restorable action can enter the ledger); `keep` only accepts files inside YOUR session dir; ledger merge dedupes main+.bak so a torn line from a crash can't lose an action. Do not work around these rails — a refused log means the log is wrong, fix the target.
- Do NOT shred/rm any path not created by the session or backed up by the ledger; the script enforces a rooted allowlist, keep using it instead of manual rm.
- Ledger is inside the session dir (it burns with everything else) — that is intended; but it means a crash loses the map. For long ops, also mirror the ledger to a second file inside the dir (script does `LEDGER.jsonl` + `.bak`).
- Work done via execute_code (not terminal) easily skips ledgering — acceptable for files staged INSIDE the session dir (burn shreds the whole dir regardless), but ANY outside-path mutation from any tool must still get a manual `incog.py log` line first.
- NEVER `log delete` a file the session itself created (e.g. a ComfyUI/tool output copy you're discarding) — burn RESTORES delete-backups, resurrecting the artifact in a persistent dir. Use `log create <path>` (reverse = idempotent delete-if-exists) or `log other`. If the mistake happens mid-window, demote the action to `other` in LEDGER.jsonl AND LEDGER.jsonl.bak (the merger dedupes by ts+action+target, so an un-patched mirror resurrects the entry), then re-check `burn --dry` before the real burn — the dry-run is what surfaces this.
- 'blacksite' while already active: reply 'already blacksite, id <x>' — don't nest.
- 'burnsite' with no active session: say so; don't glob ~/
- Memory/skill writes you 'forget' to make are the #1 leak — before finalizing any blacksite reply, re-check: did anything call memory/skill_manage/cron/git? If yes, undo it and log the undo.
- Terminal snapshots (~/.hermes/cache/scratch/hermes-snap-*) can persist exported vars across the window; harmless, but if paranoid, `reset`/fresh session at burn.
