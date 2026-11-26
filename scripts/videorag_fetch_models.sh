#!/usr/bin/env bash
# videorag_fetch_models.sh — download VideoRAG's checkpoints into the checkout.
#
# NOT run automatically: the three model sets are ~15 GB and VideoRAG cannot
# use them without a CUDA GPU (ImageBind is loaded with a hardcoded .cuda()).
# Run this once the machine has a GPU and you have accepted the licences:
#
#   bash scripts/videorag_fetch_models.sh          # asks for confirmation
#   bash scripts/videorag_fetch_models.sh --yes    # unattended
#
# Upstream resolves these paths relative to the checkout, so the layout is
# fixed:   <repo>/MiniCPM-V-2_6-int4
#          <repo>/faster-distil-whisper-large-v3
#          <repo>/.checkpoints/imagebind_huge.pth
set -euo pipefail

ROOT="${VIDEORAG_HOME:-/home/nagendra/JarvisTools/videorag}"
UPSTREAM="${VIDEORAG_ROOT:-$ROOT/VideoRAG-algorithm}"
PY="$ROOT/.venv/bin/python"
ASSUME_YES="${1:-}"

need_gb=20
free_gb=$(df -BG --output=avail "$ROOT" | tail -1 | tr -dc '0-9')
if [ "${free_gb:-0}" -lt "$need_gb" ]; then
  echo "Need ~${need_gb} GB free under $ROOT, found ${free_gb} GB." >&2
  exit 1
fi

if [ "$ASSUME_YES" != "--yes" ]; then
  cat <<EOF
This downloads ~15 GB into $UPSTREAM:
  MiniCPM-V-2_6-int4            (~8 GB, custom MiniCPM licence)
  faster-distil-whisper-large-v3 (~1.5 GB, MIT)
  imagebind_huge.pth            (~4.7 GB, CC-BY-NC — non-commercial only)
Continue? [y/N] $(printf '')
EOF
  read -r answer
  case "$answer" in y|Y|yes|YES) ;; *) echo "Aborted."; exit 0 ;; esac
fi

# huggingface_hub gives resumable downloads; git-lfs is not installed here.
hf_dl() {
  local repo="$1" dir="$2"
  if command -v hf >/dev/null 2>&1; then
    hf download "$repo" --local-dir "$dir"
  elif command -v huggingface-cli >/dev/null 2>&1; then
    huggingface-cli download "$repo" --local-dir "$dir"
  else
    "$PY" -m huggingface_hub.commands.huggingface_cli download "$repo" --local-dir "$dir"
  fi
}

echo "== MiniCPM-V-2_6-int4 =="
hf_dl openbmb/MiniCPM-V-2_6-int4 "$UPSTREAM/MiniCPM-V-2_6-int4"

echo "== faster-distil-whisper-large-v3 =="
hf_dl Systran/faster-distil-whisper-large-v3 "$UPSTREAM/faster-distil-whisper-large-v3"

echo "== ImageBind =="
mkdir -p "$UPSTREAM/.checkpoints"
if [ ! -s "$UPSTREAM/.checkpoints/imagebind_huge.pth" ]; then
  wget -c -O "$UPSTREAM/.checkpoints/imagebind_huge.pth" \
    https://dl.fbaipublicfiles.com/imagebind/imagebind_huge.pth
fi

echo
echo "Done. Verify with:"
echo "  $PY scripts/videorag_run.py status"
