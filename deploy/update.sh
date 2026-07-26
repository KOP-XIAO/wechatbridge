#!/usr/bin/env bash
# WeChatBridge upgrade script
# Usage: sudo bash deploy/update.sh
#        WECHATBRIDGE_USER=wechatbridge      # target system user (default: current user)
#        WECHATBRIDGE_SERVICE=wechatbridge    # service name to restart (default: auto-detect)
set -euo pipefail

# --- Helper: run pipx as target user ---
run_pipx() {
    if [ "$CURRENT_USER" != "$TARGET_USER" ]; then
        sudo -u "$TARGET_USER" -H pipx "$@"
    else
        pipx "$@"
    fi
}

# --- Preflight: check pipx ---
if ! command -v pipx &>/dev/null; then
    echo "ERROR: pipx not found. Install it first:"
    echo "  Debian/Ubuntu: sudo apt install pipx"
    echo "  Other:         python3 -m pip install --user pipx && python3 -m pipx ensurepath"
    exit 1
fi

# --- Determine target user ---
# Default: current user. When run as root (e.g. via sudo) and a dedicated
# 'wechatbridge' system user exists, default to that user instead — upgrading
# root's pipx copy would not affect the service.
if [ -n "${WECHATBRIDGE_USER:-}" ]; then
    TARGET_USER="$WECHATBRIDGE_USER"
elif [ "$(id -u)" -eq 0 ] && id wechatbridge &>/dev/null; then
    TARGET_USER="wechatbridge"
else
    TARGET_USER="$(whoami)"
fi
CURRENT_USER="$(whoami)"

echo "Target user: $TARGET_USER"
if [ "$CURRENT_USER" != "$TARGET_USER" ]; then
    echo "(pipx will run as '$TARGET_USER'; override with WECHATBRIDGE_USER=<user>)"
fi

# --- Upgrade (or install if not present) ---
echo "Upgrading WeChatBridge..."
if run_pipx list 2>/dev/null | grep -q "wechatbridge"; then
    run_pipx upgrade wechatbridge
else
    echo "WeChatBridge not yet installed under '$TARGET_USER'. Installing..."
    run_pipx install wechatbridge
fi

# --- Print new version ---
echo "---"
if [ "$CURRENT_USER" != "$TARGET_USER" ]; then
    sudo -u "$TARGET_USER" -H wechatbridge --version 2>/dev/null || echo "(could not determine version)"
else
    wechatbridge --version 2>/dev/null || echo "(could not determine version)"
fi
echo "---"

# --- Restart systemd services ---
if command -v systemctl &>/dev/null; then
    SUDO_CMD=""
    if [ "$(id -u)" -ne 0 ]; then
        SUDO_CMD="sudo"
        echo "Not running as root, will use sudo for systemctl."
    fi

    # If a specific service was requested, restart only that
    if [ -n "${WECHATBRIDGE_SERVICE:-}" ]; then
        echo "Restarting service '$WECHATBRIDGE_SERVICE'..."
        $SUDO_CMD systemctl restart "$WECHATBRIDGE_SERVICE" || echo "  (service not found, skipping)"
    else
        # Restart the main service if it exists
        if $SUDO_CMD systemctl list-unit-files wechatbridge.service &>/dev/null; then
            echo "Restarting wechatbridge.service..."
            $SUDO_CMD systemctl restart wechatbridge.service || echo "  (not found, skipping)"
        fi

        # Restart all enabled or active template instances
        echo "Looking for wechatbridge@*.service instances..."
        mapfile -t INSTANCES < <($SUDO_CMD systemctl list-units --all 'wechatbridge@*.service' --no-legend 2>/dev/null | awk '{print $1}' | sort -u || true)
        if [ ${#INSTANCES[@]} -gt 0 ]; then
            for svc in "${INSTANCES[@]}"; do
                echo "Restarting $svc..."
                $SUDO_CMD systemctl restart "$svc" || echo "  (failed to restart $svc)"
            done
        else
            echo "  No wechatbridge@*.service instances found."
        fi
    fi
else
    echo "systemctl not found. Please restart WeChatBridge manually."
fi

# --- Done ---
echo ""
echo "=== WeChatBridge upgrade complete ==="
echo "To watch logs:"
echo "  sudo journalctl -u wechatbridge -f"
echo ""
echo "For multi-instance setups, replace 'wechatbridge' with the specific service name."
