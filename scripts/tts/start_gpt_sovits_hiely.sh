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
approved_gpt_checkpoint="${GPT_SOVITS_GPT_CHECKPOINT:-$gpt_sovits_root/GPT_weights_v2ProPlus/hiely-e25.ckpt}"
approved_sovits_checkpoint="${GPT_SOVITS_SOVITS_CHECKPOINT:-$gpt_sovits_root/SoVITS_weights_v2ProPlus/hiely_e80_s12960.pth}"
approved_gpt_sha256="${GPT_SOVITS_GPT_SHA256:-061f0a8a1658f0b61a0654c976fd7e137284d5deefb24219c4b99337ef80ea68}"
approved_sovits_sha256="${GPT_SOVITS_SOVITS_SHA256:-52cb12ae0c2140c54e7e20d4c3cf5c785dc78d03d99eb1f96299a0c6c2331486}"
latest_dir="${GPT_SOVITS_LATEST_DIR:-$gpt_sovits_root/latest}"
infer_config="${GPT_SOVITS_CONFIG:-GPT_SoVITS/configs/tts_infer_hiely.yaml}"

cd "$gpt_sovits_root"
mkdir -p "$latest_dir"

resolve_checkpoint() { # $1=approved path $2=approved sha256 $3=weight kind
  local approved="$1" expected_sha256="${2,,}" weight_kind="$3"
  local resolved actual_sha256
  if [[ -z "$approved" || -z "$expected_sha256" ]]; then
    echo "[hiely-launcher] $weight_kind checkpoint path and SHA-256 are required" >&2
    return 1
  fi
  if [[ ! -f "$approved" ]]; then
    echo "[hiely-launcher] approved $weight_kind checkpoint does not exist: $approved" >&2
    return 1
  fi
  resolved=$(readlink -f "$approved")
  actual_sha256=$(sha256sum -- "$resolved")
  actual_sha256="${actual_sha256%% *}"
  if [[ "${actual_sha256,,}" != "$expected_sha256" ]]; then
    echo "[hiely-launcher] approved $weight_kind checkpoint SHA-256 mismatch" >&2
    return 1
  fi
  printf '%s\n' "$resolved"
}

gpt_target=$(resolve_checkpoint "$approved_gpt_checkpoint" "$approved_gpt_sha256" gpt)
sovits_target=$(resolve_checkpoint "$approved_sovits_checkpoint" "$approved_sovits_sha256" sovits)
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
