#!/usr/bin/env bash
# verify_paper_key.sh — validate a hand-copied paper key against the stored
# fingerprint (sha256). The key itself is typed interactively and NEVER
# written to disk; only the constant fingerprint below is stored.
set -euo pipefail
FP_STORED="0e3db83b066aed82"   # first 16 hex of sha256(key), 2026-09-23
read -r -p "Type the 64-char key from paper: " K
FP="$(printf '%s' "$K" | sha256sum | cut -c1-16)"
if [ "$FP" = "$FP_STORED" ]; then
  echo "MATCH ✓ paper copy is correct"
  echo "To rebuild the key file:  printf '%s\n' \"\$K\" > ~/.backup_offsite.key && chmod 600 ~/.backup_offsite.key"
else
  echo "MISMATCH ✗ (got $FP — recheck ambiguous chars: 0/O, 1/l/I, +/t, //7)"
  exit 1
fi
