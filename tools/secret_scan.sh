#!/usr/bin/env bash
# R5: scan the WHOLE git history (every commit, every branch) for credentials.
# Prints only the commit and file of a hit, never the matched value.
set -uo pipefail
cd "$(dirname "$0")/.."
PATTERNS=(
  'PAYPAL_CLIENT_(SECRET|ID)=[A-Za-z0-9_-]{8,}'   # a filled-in PayPal key
  'A21AA[A-Za-z0-9_-]{20,}'                         # PayPal OAuth access token
  'sk-ant-[A-Za-z0-9_-]{10,}'                       # Anthropic API key
  'Bearer [A-Za-z0-9._-]{24,}'                      # any bearer token
  'access_token"?[:=] *"[A-Za-z0-9._-]{20,}'        # token in JSON / env form
  '-----BEGIN [A-Z ]*PRIVATE KEY-----'
)
hits=0
for pat in "${PATTERNS[@]}"; do
  # git grep across every commit in history; -l prints only commit:path
  revs=$(git rev-list --all)
  [ -z "$revs" ] && continue
  out=$(git grep -l -E -e "$pat" $revs -- . 2>/dev/null | sort -u)
  if [ -n "$out" ]; then
    echo "SECRET PATTERN '${pat%%[*}…' found in:"; echo "$out" | sed 's/^/  /'; hits=$((hits+1))
  fi
done
if [ "$hits" -eq 0 ]; then
  echo "secret scan: clean ($(git rev-list --all | wc -l | tr -d ' ') commits, ${#PATTERNS[@]} patterns)"
  exit 0
fi
exit 1
