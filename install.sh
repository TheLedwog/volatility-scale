#!/usr/bin/env bash
#
# Trade / Don't-Trade Scale - one-shot installer for Raspberry Pi / Linux.
#
# Run it from the repo (no sudo needed; it elevates only the steps that require it):
#
#     bash install.sh
#
# It will: install system + Python deps, securely save your OpenAI API key,
# seed the news cache, build the model, and install + start a systemd service
# that runs the app on boot and fires the predict/label jobs automatically (in ET).
#
# Re-running it is safe (idempotent): it keeps your existing key/data unless you
# choose to replace them.
set -euo pipefail

APP_NAME="tradescale"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/.env"
SERVICE_PATH="/etc/systemd/system/${APP_NAME}.service"
cd "$SCRIPT_DIR"

# Who should own files / run the service (the real user, even under sudo).
RUN_USER="${SUDO_USER:-$(id -un)}"
# Elevate only when we are not already root.
if [ "$(id -u)" -eq 0 ]; then SUDO=""; else SUDO="sudo"; fi

echo "=============================================================="
echo " Installing $APP_NAME"
echo "   directory : $SCRIPT_DIR"
echo "   run as    : $RUN_USER"
echo "=============================================================="

# --- 1. System packages (best effort; skipped if apt is unavailable) ----------
if command -v apt-get >/dev/null 2>&1; then
  echo "==> Installing system packages (python3-venv, python3-pip, git)..."
  $SUDO apt-get update -qq || true
  $SUDO apt-get install -y python3-venv python3-pip git >/dev/null || true
fi

# --- 2. Virtualenv + Python dependencies --------------------------------------
if [ ! -d "$SCRIPT_DIR/.venv" ]; then
  echo "==> Creating virtualenv (.venv)..."
  python3 -m venv "$SCRIPT_DIR/.venv"
fi
echo "==> Installing Python dependencies (this can take a few minutes on a Pi)..."
"$SCRIPT_DIR/.venv/bin/pip" install --upgrade pip -q
"$SCRIPT_DIR/.venv/bin/pip" install -r "$SCRIPT_DIR/requirements.txt"

# --- 3. OpenAI API key -> .env (secure: chmod 600, never committed) -----------
set_env_var() {  # set_env_var FILE KEY VALUE  (preserves other lines)
  local file="$1" key="$2" val="$3"
  touch "$file"
  if grep -q "^${key}=" "$file" 2>/dev/null; then
    grep -v "^${key}=" "$file" > "$file.tmp" && mv "$file.tmp" "$file"
  fi
  printf '%s=%s\n' "$key" "$val" >> "$file"
}

SKIP_KEY=""
if [ -f "$ENV_FILE" ] && grep -q '^OPENAI_API_KEY=.\+' "$ENV_FILE" 2>/dev/null; then
  read -r -p "==> An OpenAI API key is already saved. Replace it? [y/N]: " ans </dev/tty
  [[ "${ans:-}" =~ ^[Yy] ]] || SKIP_KEY=1
fi

if [ -z "$SKIP_KEY" ]; then
  echo "==> Enter your OpenAI API key for live news scoring."
  echo "    (input is hidden; press Enter to skip - headlines still work, no GPT read)"
  read -r -s -p "    OpenAI API key: " OPENAI_KEY </dev/tty
  echo
  if [ -n "${OPENAI_KEY:-}" ]; then
    set_env_var "$ENV_FILE" OPENAI_API_KEY "$OPENAI_KEY"
    chmod 600 "$ENV_FILE"
    echo "    saved to .env (permissions 600, git-ignored)"
    echo "==> Verifying the key with OpenAI..."
    if OPENAI_API_KEY="$OPENAI_KEY" "$SCRIPT_DIR/.venv/bin/python" \
         -c "from openai import OpenAI; OpenAI().models.list()" >/dev/null 2>&1; then
      echo "    key works [ok]"
    else
      echo "    could not verify the key (it's saved anyway; you can re-check in Settings)"
    fi
    unset OPENAI_KEY
  else
    echo "    no key entered - skipping (you can re-run install.sh later to add one)"
  fi
fi

# --- 4. Port ------------------------------------------------------------------
read -r -p "==> Port to serve the web UI on [8001]: " PORT </dev/tty || true
PORT="${PORT:-8001}"

# --- 5. Initialise DB, import the shipped news cache, build the model ---------
echo "==> Seeding the news cache from data/news_seed.csv..."
"$SCRIPT_DIR/.venv/bin/python" -m app.ml.seed_news import || true
echo "==> Building the model from data/training.csv (no key/internet needed)..."
"$SCRIPT_DIR/.venv/bin/python" -m app.ml.train || \
  echo "    (model build skipped/failed - the app still runs on the rule engine)"

# If we ran as root, hand ownership of the generated files back to the user.
if [ "$(id -u)" -eq 0 ] && [ "$RUN_USER" != "root" ]; then
  chown -R "$RUN_USER":"$RUN_USER" "$SCRIPT_DIR/.venv" "$SCRIPT_DIR/data" "$ENV_FILE" 2>/dev/null || true
fi

# --- 6. systemd service (runs on boot; scheduler fires predict/label in ET) ---
echo "==> Installing systemd service ($SERVICE_PATH)..."
$SUDO tee "$SERVICE_PATH" >/dev/null <<EOF
[Unit]
Description=Trade / Don't-Trade Scale
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$SCRIPT_DIR
Environment=PORT=$PORT
EnvironmentFile=-$ENV_FILE
ExecStart=$SCRIPT_DIR/.venv/bin/python run.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

$SUDO systemctl daemon-reload
$SUDO systemctl enable --now "$APP_NAME"

# --- 7. Done ------------------------------------------------------------------
IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo
echo "=============================================================="
echo " Installed and running."
echo "   Web UI    : http://${IP:-<pi-ip>}:$PORT"
echo "   Service   : systemctl status $APP_NAME"
echo "   Logs      : journalctl -u $APP_NAME -f"
echo "   API key   : stored in $ENV_FILE (chmod 600). Re-run install.sh to change."
echo "   Uninstall : bash uninstall.sh   (add --purge to also delete the local DB)"
echo "=============================================================="
