#!/usr/bin/env bash
# Supervised GPT-SoVITS launcher for the local Hiely voice deployment.
#
# The launcher remains the process-group owner instead of replacing itself with
# api_v2. This is required under WSL: an interop relay may otherwise report the
# managed handle as exited while the real API is re-parented to /init, leaking
# port 9880 and GPU memory after the plugin reports a successful shutdown.
set -euo pipefail

gpt_sovits_root="${GPT_SOVITS_ROOT:-/root/GPT-SoVITS}"
listen_address="${GPT_SOVITS_ADDRESS:-127.0.0.1}"
listen_port="${GPT_SOVITS_PORT:-9880}"
stable_age_seconds="${GPT_SOVITS_STABLE_AGE_SECONDS:-180}"
approved_gpt_checkpoint="${GPT_SOVITS_GPT_CHECKPOINT:-}"
approved_sovits_checkpoint="${GPT_SOVITS_SOVITS_CHECKPOINT:-}"
latest_dir="${GPT_SOVITS_LATEST_DIR:-$gpt_sovits_root/latest}"
infer_config="${GPT_SOVITS_CONFIG:-GPT_SoVITS/configs/tts_infer_hiely.yaml}"

cd "$gpt_sovits_root"
mkdir -p "$latest_dir"

now=$(date +%s)
pick_stable_checkpoint() { # $1=directory $2=glob
  local best="" best_time=0 candidate candidate_time
  while IFS= read -r candidate; do
    [[ -z "$candidate" ]] && continue
    candidate_time=$(stat -c %Y "$candidate")
    (( now - candidate_time < stable_age_seconds )) && continue
    if (( candidate_time > best_time )); then
      best_time=$candidate_time
      best="$candidate"
    fi
  done < <(find "$1" -maxdepth 1 -type f -name "$2" -printf '%T@ %p\n' 2>/dev/null \
    | sort -nr | cut -d' ' -f2-)
  if [[ -z "$best" ]]; then
    echo "[hiely-launcher] no stable checkpoint in $1/$2" >&2
    return 1
  fi
  readlink -f "$best"
}

resolve_checkpoint() { # $1=approved path $2=fallback directory $3=fallback glob
  local approved="$1"
  if [[ -n "$approved" ]]; then
    if [[ ! -f "$approved" ]]; then
      echo "[hiely-launcher] approved checkpoint does not exist: $approved" >&2
      return 1
    fi
    readlink -f "$approved"
    return 0
  fi
  echo "[hiely-launcher] no approved checkpoint supplied; using newest stable fallback" >&2
  pick_stable_checkpoint "$2" "$3"
}

gpt_target=$(resolve_checkpoint "$approved_gpt_checkpoint" GPT_weights_v2ProPlus 'hiely-e*.ckpt')
sovits_target=$(resolve_checkpoint "$approved_sovits_checkpoint" SoVITS_weights_v2ProPlus 'hiely_e*_s*.pth')
gpt_link="$latest_dir/hiely-gpt.ckpt"
sovits_link="$latest_dir/hiely-sovits.pth"
ln -sfn "$gpt_target" "$gpt_link"
ln -sfn "$sovits_target" "$sovits_link"

python3 - "$infer_config" "$gpt_link" "$sovits_link" <<'PY'
from __future__ import annotations

import pathlib
import sys

config_path, gpt_path, sovits_path = sys.argv[1:4]
path = pathlib.Path(config_path)
lines = path.read_text(encoding="utf-8").splitlines()
replacement = {
    "t2s_weights_path": gpt_path,
    "vits_weights_path": sovits_path,
}
updated: list[str] = []
inside_custom = False
replaced: set[str] = set()
for line in lines:
    if line.startswith("custom:"):
        inside_custom = True
    elif line.endswith(":") and not line.startswith(" "):
        inside_custom = False
    if inside_custom:
        key = line.strip().split(":", 1)[0]
        if key in replacement:
            indent = line[: len(line) - len(line.lstrip())]
            updated.append(f"{indent}{key}: {replacement[key]}")
            replaced.add(key)
            continue
    updated.append(line)
missing = replacement.keys() - replaced
if missing:
    raise SystemExit(f"[hiely-launcher] custom section missing keys: {sorted(missing)}")
path.write_text("\n".join(updated) + "\n", encoding="utf-8")
print(f"[hiely-launcher] gpt link -> {gpt_path}")
print(f"[hiely-launcher] sovits link -> {sovits_path}")
PY

if [[ "${1:-}" == "--dry-config" ]]; then
  echo "[hiely-launcher] dry-config only, not starting service"
  exit 0
fi

child_pid=0
cleanup() {
  local status=$?
  trap - EXIT INT TERM
  if (( child_pid > 0 )) && kill -0 "$child_pid" 2>/dev/null; then
    kill -TERM "$child_pid" 2>/dev/null || true
    for _ in {1..20}; do
      kill -0 "$child_pid" 2>/dev/null || break
      sleep 0.25
    done
    if kill -0 "$child_pid" 2>/dev/null; then
      kill -KILL "$child_pid" 2>/dev/null || true
    fi
    wait "$child_pid" 2>/dev/null || true
  fi
  exit "$status"
}
trap cleanup EXIT INT TERM

.venv/bin/python -u api_v2.py \
  -a "$listen_address" \
  -p "$listen_port" \
  -c "$infer_config" &
child_pid=$!
wait "$child_pid"
