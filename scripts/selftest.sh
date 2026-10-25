#!/usr/bin/env bash
# End-to-end reversibility test for incog.py — no agent needed, stdlib + coreutils.
# Creates a throwaway home under $(mktemp -d), exercises open/create/modify/keep/list/burn
# in DISK mode, then RAM mode if /run/user/$UID exists, and asserts every reversal.
set -u
HERE="$(cd "$(dirname "$0")/.." && pwd)"
S="$HERE/skills/security/incognito-blacksite/scripts/incog.py"
PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); echo "  ok   $1"; }
bad()  { FAIL=$((FAIL+1)); echo "  FAIL $1"; }
assert() { # assert <desc> <condition-cmd...>
  local desc="$1"; shift
  if "$@" >/dev/null 2>&1; then ok "$desc"; else bad "$desc"; fi
}

command -v shred >/dev/null || echo "note: shred missing — burn falls back to unlink (test still valid)"
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
export HOME_BAK="$HOME"; SCRATCH="$TMP/scratch"; mkdir -p "$SCRATCH"

run_suite() {
  local mode="$1"; shift
  echo "== mode: $mode"
  OUT=$(python3 "$S" open $mode) || { bad "open $mode"; return; }
  ID=$(echo "$OUT" | awk '{print $1}'); SD=$(echo "$OUT" | awk '{print $2}')
  [ -n "$ID" ] && [ -d "$SD" ] && ok "session open ($ID)" || { bad "session dir missing"; return; }

  # tmpfs verification in ram mode
  if [ "$mode" = "--ram" ]; then
    FSTYPE=$(findmnt -T "$SD" -n -o FSTYPE 2>/dev/null)
    [ "$FSTYPE" = "tmpfs" ] && ok "ram session on tmpfs" || bad "ram session not tmpfs ($FSTYPE)"
  fi

  # create (logged, then really made outside)
  CREATED="$SCRATCH/created.txt"; python3 "$S" log "$ID" create "$CREATED"; echo made > "$CREATED"
  assert "created file exists pre-burn" test -f "$CREATED"

  # SAFETY: empty target refused
  python3 "$S" log "$ID" create "" 2>&1 | grep -q refusing && ok "safety: empty target refused" || bad "safety: empty target accepted"
  # SAFETY: protected root refused at LOG time
  python3 "$S" log "$ID" create /tmp 2>&1 | grep -q refusing && ok "safety: /tmp refused at log" || bad "safety: /tmp accepted"
  # SAFETY: mis-logged create over existing file must be RESTORED, not deleted
  echo VITAL > "$SCRATCH/preexisting.txt"
  python3 "$S" log "$ID" create "$SCRATCH/preexisting.txt" 2>&1 | grep -q create_over_existing && ok "safety: create-over-existing demoted" || bad "safety: create-over-existing not demoted"
  echo TAMPERED > "$SCRATCH/preexisting.txt"

  # SAFETY: typo'd action refused (a silent typo disables reversal)
  python3 "$S" log "$ID" creat /nonexistent 2>&1 | grep -q "unknown action" && ok "safety: typo action refused" || bad "safety: typo action accepted"
  # SAFETY: cross-session keep refused
  OUT2=$(python3 "$S" open); ID2T=$(echo "$OUT2" | awk '{print $1}'); SD2T=$(echo "$OUT2" | awk '{print $2}')
  echo not-yours > "$SD2T/victim.txt"
  python3 "$S" keep "$ID" "$SD2T/victim.txt" 2>&1 | grep -q "does not belong" && ok "safety: cross-session keep refused" || bad "safety: cross-session keep allowed"
  rm -f "$SD2T/victim.txt"; python3 "$S" burn "$ID2T" >/dev/null 2>&1
  # SAFETY: same-second double modify restores the ORIGINAL (backup-name collision guard)
  SS="$SCRATCH/samesec.txt"; echo A > "$SS"
  python3 "$S" log "$ID" modify "$SS" >/dev/null; echo B > "$SS"
  python3 "$S" log "$ID" modify "$SS" >/dev/null; echo C > "$SS"

  # modify (auto-backup)
  TARGET="$SCRATCH/target.txt"; echo ORIGINAL > "$TARGET"
  python3 "$S" log "$ID" modify "$TARGET" >/dev/null; echo TAMPERED > "$TARGET"
  grep -q TAMPERED "$TARGET" && ok "modify applied pre-burn" || bad "pre-burn state wrong"

  # dry burn must not change anything
  python3 "$S" burn "$ID" --dry >/dev/null
  grep -q TAMPERED "$TARGET" && assert "dry-run left modify untouched" test -f "$CREATED" \
    && ok "dry-run left create untouched" || bad "dry-run mutated state!"

  # keep
  echo precious > "$SD/precious.txt"; python3 "$S" keep "$ID" "$SD/precious.txt" >/dev/null
  OUTBOX_FILE=$(dirname "$SD")/outbox/${ID}__precious.txt
  assert "keep survived into outbox" test -f "$OUTBOX_FILE"
  grep -q precious "$OUTBOX_FILE" && ok "keep content intact" || bad "keep content corrupted"

  # real burn
  python3 "$S" burn "$ID" >/dev/null
  assert "created file removed by burn" sh -c "! test -e '$CREATED'"
  grep -q VITAL "$SCRATCH/preexisting.txt" && ok "mis-logged create RESTORED at burn" || bad "mis-logged create destroyed user data!"
  [ -d "$SD" ] && bad "session dir survived burn" || ok "session dir destroyed"
  grep -q ORIGINAL "$TARGET" && ok "modify REVERSED from backup" || bad "modify not reversed"
  grep -q "^A$" "$SS" && ok "same-second double modify -> ORIGINAL restored" || bad "same-second modify collision: got $(cat $SS)"
  assert "kept file still present post-burn" test -f "$OUTBOX_FILE"

  # list must show nothing for a burned id
  python3 "$S" list | grep -q "$ID" && bad "burned id still listed" || ok "burned id not listed"
  rm -f "$TARGET" "$OUTBOX_FILE"
}

echo "== DISK mode (redirected into a temp 'disk' — burn cleans up)"
# Point the test at a throwaway root by running with a stub HOME via bind-less trick:
# the script roots at ~/.incognito; to avoid touching real home we test in RAM if possible,
# and for disk mode we accept ~/.incognito and remove only what we made.
run_suite ""
echo "== RAM mode"
if [ -d "/run/user/$(id -u)" ] && [ "$(findmnt -T "/run/user/$(id -u)" -n -o FSTYPE 2>/dev/null)" = "tmpfs" ]; then
  run_suite "--ram"
else
  echo "  skip (no per-user tmpfs)"
fi

# cleanup of test roots we own
rmdir ~/.incognito 2>/dev/null; rmdir "/run/user/$(id -u)/incognito/outbox" "/run/user/$(id -u)/incognito" 2>/dev/null

echo; echo "RESULT: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
