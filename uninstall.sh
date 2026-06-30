#!/usr/bin/env bash
#
# Trade / Don't-Trade Scale - uninstaller. Removes the service, any cron jobs,
# the virtualenv and the stored API key. Your accumulated data (the SQLite DB and
# trained model) is KEPT by default; pass --purge to delete it too.
#
#     bash uninstall.sh            # remove service/cron/venv/key, keep data
#     bash uninstall.sh --purge    # ...and delete the local DB + model as well
#
# The source code (this git checkout) is left in place either way - delete the
# folder yourself if you want it gone entirely.
set -euo pipefail

APP_NAME="tradescale"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/.env"
SERVICE_PATH="/etc/systemd/system/${APP_NAME}.service"

RUN_USER="${SUDO_USER:-$(id -un)}"
if [ "$(id -u)" -eq 0 ]; then SUDO=""; else SUDO="sudo"; fi

PURGE=0
[ "${1:-}" = "--purge" ] && PURGE=1

echo "==> Uninstalling $APP_NAME from $SCRIPT_DIR"

# --- 1. systemd service -------------------------------------------------------
if [ -f "$SERVICE_PATH" ] || systemctl list-unit-files 2>/dev/null | grep -q "^${APP_NAME}.service"; then
  $SUDO systemctl disable --now "$APP_NAME" 2>/dev/null || true
  $SUDO rm -f "$SERVICE_PATH"
  $SUDO systemctl daemon-reload 2>/dev/null || true
  echo "    removed systemd service"
fi

# --- 2. cron jobs that reference this project (for the run user and root) ------
clean_cron() {
  local who="$1" cur new
  cur="$($SUDO crontab -u "$who" -l 2>/dev/null || true)"
  [ -n "$cur" ] || return 0
  new="$(printf '%s\n' "$cur" | grep -vF \
        -e 'app.jobs.predict' -e 'app.jobs.label' -e "$SCRIPT_DIR" || true)"
  if [ "$new" != "$cur" ]; then
    printf '%s\n' "$new" | $SUDO crontab -u "$who" -
    echo "    cleaned cron entries for $who"
  fi
}
clean_cron "$RUN_USER"
[ "$RUN_USER" != "root" ] && clean_cron root || true

# --- 3. virtualenv ------------------------------------------------------------
if [ -d "$SCRIPT_DIR/.venv" ]; then
  rm -rf "$SCRIPT_DIR/.venv"
  echo "    removed .venv"
fi

# --- 4. API key (secure delete) ----------------------------------------------
if [ -f "$ENV_FILE" ]; then
  if command -v shred >/dev/null 2>&1; then shred -u "$ENV_FILE"; else rm -f "$ENV_FILE"; fi
  echo "    removed .env (API key)"
fi

# --- 5. data (history + model) - only with --purge ---------------------------
if [ "$PURGE" -eq 1 ]; then
  rm -f "$SCRIPT_DIR/data/tradescale.db" "$SCRIPT_DIR/data/model.joblib"
  echo "    purged local DB + trained model (kept committed seed/training.csv)"
else
  echo "    kept data/ (your prediction history + model). Re-run with --purge to delete."
fi

echo "==> Done. Source left intact; 'rm -rf \"$SCRIPT_DIR\"' to remove it entirely."
