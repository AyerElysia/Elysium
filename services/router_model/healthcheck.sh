#!/usr/bin/env bash
# 路由模型服务健康检查 + 真实延迟测量。
#
# 这个服务的存在意义就是低延迟，所以光看 /health 返回 200 不够，
# 得量一次真实 completion 的耗时。
#
# 用法：
#   ./healthcheck.sh          # 查一次：/health + /v1/models + 一次真实请求延迟
#   ./healthcheck.sh --wait   # 等到就绪（冷启动时用，最长 10 分钟）
#   ./healthcheck.sh --bench  # 连打 5 次，看首字延迟的稳定情况

set -uo pipefail

PORT="${ROUTER_PORT:-8849}"
SERVED_NAME="${ROUTER_SERVED_NAME:-qwen3.5-2b-router}"
BASE="http://127.0.0.1:$PORT"
MODE="${1:-}"

ok()   { printf '  \033[32m✓\033[0m %s\n' "$1"; }
bad()  { printf '  \033[31m✗\033[0m %s\n' "$1"; }

probe_health() {
    curl -sf -m 3 -o /dev/null "$BASE/health"
}

# 一次真实的短请求，返回耗时（毫秒）。路由判定就是这种形状：短输入短输出。
measure_latency() {
    local body start end
    body=$(cat <<JSON
{"model":"$SERVED_NAME",
 "messages":[{"role":"user","content":"只回一个字：好"}],
 "max_tokens":8,"temperature":0,"stream":false}
JSON
)
    start=$(date +%s%3N)
    if ! curl -sf -m 60 "$BASE/v1/chat/completions" \
        -H 'Content-Type: application/json' -d "$body" >/tmp/.router_hc.json 2>/dev/null; then
        return 1
    fi
    end=$(date +%s%3N)
    echo $(( end - start ))
}

if [ "$MODE" = "--wait" ]; then
    echo "==> 等待服务就绪（$BASE，最长 600s）"
    for i in $(seq 1 600); do
        if probe_health; then
            ok "服务已就绪（等待 ${i}s）"
            break
        fi
        if [ "$i" -eq 600 ]; then
            bad "10 分钟仍未就绪，看日志：journalctl -u router-model -n 50"
            exit 1
        fi
        sleep 1
    done
fi

echo "==> 检查 $BASE"

if ! probe_health; then
    bad "/health 不通"
    if command -v systemctl >/dev/null 2>&1 && systemctl is-enabled router-model >/dev/null 2>&1; then
        echo "     状态: $(systemctl is-active router-model)"
        echo "     日志: journalctl -u router-model -n 50"
    else
        echo "     服务没在跑，也没装 systemd unit。装一下: sudo ./install_service.sh"
    fi
    exit 1
fi
ok "/health 200"

MODELS=$(curl -sf -m 3 "$BASE/v1/models" 2>/dev/null)
if [ -z "$MODELS" ]; then
    bad "/v1/models 无响应"
    exit 1
fi
if printf '%s' "$MODELS" | grep -q "\"$SERVED_NAME\""; then
    ok "/v1/models 含 $SERVED_NAME"
else
    bad "/v1/models 里没有 $SERVED_NAME，config/models.toml 的 model id 会对不上"
    printf '     实际返回: %s\n' "$MODELS"
    exit 1
fi

if [ "$MODE" = "--bench" ]; then
    echo "==> 连打 5 次（第 1 次含 prefix cache 冷启动，偏慢是正常的）"
    for i in 1 2 3 4 5; do
        ms=$(measure_latency) || { bad "第 $i 次请求失败"; exit 1; }
        printf '  #%d  %sms\n' "$i" "$ms"
    done
else
    ms=$(measure_latency) || { bad "chat/completions 请求失败"; exit 1; }
    ok "一次真实请求 ${ms}ms"
    if [ "$ms" -gt 3000 ]; then
        echo "     偏慢。检查是不是退回了 --enforce-eager，或者显存不够在换页"
    fi
fi

rm -f /tmp/.router_hc.json
echo "==> OK"
