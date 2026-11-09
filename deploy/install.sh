#!/usr/bin/env bash
# Instalace / aktualizace Calendar Sync na čistém Debianu/Ubuntu (VM, LXC, bare metal) bez Dockeru.
# Spuštění (jako root, z kořene repozitáře):   sudo ./deploy/install.sh [PUBLIC_BASE_URL] [PORT]
#   PUBLIC_BASE_URL  adresa, na které se k aplikaci přistupuje, např. http://192.0.2.10:8080 (výchozí http://localhost:8080)
#   PORT             port aplikace (výchozí 8080)
# Heslo správce si ZVOLÍTE: skript se na ně zeptá (min. 12 znaků), nebo ho předejte v proměnné ADMIN_PASSWORD
# (např. pro automatizaci):   sudo ADMIN_PASSWORD='...' ./deploy/install.sh ...
# Skript je idempotentní: znovu spuštěn aktualizuje kód a zachová data, tajné soubory, heslo i nastavení.
# Změnit heslo později jde v aplikaci (Nastavení → Heslo správce) nebo:  sudo -u calsync ... calendar-sync set-password
set -euo pipefail

BASE_URL="${1:-http://localhost:8080}"
PORT="${2:-8080}"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP=/opt/calendar-sync
ETC=/etc/calendar-sync
DATA=/var/lib/calendar-sync
UNIT=/etc/systemd/system/calendar-sync.service

[ "$(id -u)" -eq 0 ] || { echo "Spusťte jako root (sudo)." >&2; exit 1; }
[ -f "$SRC/pyproject.toml" ] || { echo "Nenalezen pyproject.toml - spouštějte z klonu repozitáře." >&2; exit 1; }

echo "==> Závislosti systému"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip openssl ca-certificates >/dev/null

echo "==> Uživatel a adresáře"
id calsync >/dev/null 2>&1 || useradd --system --home-dir "$DATA" --shell /usr/sbin/nologin calsync
mkdir -p "$APP" "$ETC" "$DATA"

echo "==> Aplikace do $APP"
rm -rf "$APP/src"
mkdir -p "$APP/src"
tar -C "$SRC" --exclude=.git --exclude=.venv --exclude=.local --exclude=data --exclude=secrets --exclude=__pycache__ \
    --exclude='*.egg-info' -cf - . | tar -C "$APP/src" --no-same-owner -xf -
[ -x "$APP/venv/bin/python" ] || python3 -m venv "$APP/venv"
"$APP/venv/bin/pip" install -q --upgrade pip
"$APP/venv/bin/pip" install -q --upgrade "$APP/src"
rm -rf "$APP/src/build"

echo "==> Tajné soubory (existující se nepřepisují)"
if [ ! -s "$ETC/session_secret.txt" ]; then
  openssl rand -base64 48 | tr -d '\n' > "$ETC/session_secret.txt"
fi
CHOSEN_PASSWORD=""
if [ -n "${ADMIN_PASSWORD:-}" ]; then
  CHOSEN_PASSWORD="$ADMIN_PASSWORD"
elif [ ! -s "$ETC/admin_password.txt" ]; then
  if [ ! -t 0 ]; then
    echo "Heslo správce nebylo zadáno. Spusťte skript interaktivně, nebo nastavte proměnnou ADMIN_PASSWORD." >&2
    exit 1
  fi
  while :; do
    read -r -s -p "Zvolte heslo správce (min. 12 znaků): " P1; echo
    read -r -s -p "Heslo znovu: " P2; echo
    if [ "${#P1}" -lt 12 ]; then echo "Heslo je příliš krátké."; continue; fi
    if [ "$P1" != "$P2" ]; then echo "Hesla se neshodují."; continue; fi
    CHOSEN_PASSWORD="$P1"; break
  done
fi
if [ -n "$CHOSEN_PASSWORD" ]; then
  [ "${#CHOSEN_PASSWORD}" -ge 12 ] || { echo "Heslo musí mít alespoň 12 znaků." >&2; exit 1; }
  printf '%s' "$CHOSEN_PASSWORD" > "$ETC/admin_password.txt"
fi
chown -R calsync:calsync "$ETC" "$DATA"
chmod 700 "$ETC"
chmod 600 "$ETC"/* 2>/dev/null || true
if [ -n "$CHOSEN_PASSWORD" ]; then
  # Uloží hash hesla do databáze (přebíjí případně starší heslo změněné v aplikaci).
  printf '%s
' "$CHOSEN_PASSWORD" | runuser -u calsync -- env DATA_DIR="$DATA" ADMIN_PASSWORD_FILE="$ETC/admin_password.txt"     SESSION_SECRET_FILE="$ETC/session_secret.txt" "$APP/venv/bin/calendar-sync" set-password
fi
unset CHOSEN_PASSWORD P1 P2

echo "==> Služba systemd"
COOKIE_SECURE=false
case "$BASE_URL" in https://*) COOKIE_SECURE=true ;; esac
cat > "$UNIT" <<UNIT_EOF
[Unit]
Description=Calendar Sync
After=network-online.target
Wants=network-online.target

[Service]
User=calsync
Group=calsync
WorkingDirectory=$APP
Environment=DATA_DIR=$DATA
Environment=COOKIE_SECURE=$COOKIE_SECURE
Environment=PUBLIC_BASE_URL=$BASE_URL
Environment=ADMIN_PASSWORD_FILE=$ETC/admin_password.txt
Environment=SESSION_SECRET_FILE=$ETC/session_secret.txt
Environment=GOOGLE_OAUTH_CLIENT_FILE=$ETC/google_oauth_client.json
ExecStart=$APP/venv/bin/uvicorn calendar_sync.web:create_app --factory --host 0.0.0.0 --port $PORT --no-proxy-headers
Restart=on-failure
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=$DATA
PrivateTmp=true

[Install]
WantedBy=multi-user.target
UNIT_EOF
systemctl daemon-reload
systemctl enable calendar-sync >/dev/null
systemctl restart calendar-sync
sleep 4
systemctl is-active --quiet calendar-sync || { journalctl -u calendar-sync -n 30 --no-pager >&2; exit 1; }
"$APP/venv/bin/python" -c "import urllib.request as u; print('healthz:', u.urlopen('http://127.0.0.1:$PORT/healthz').read().decode())"

echo
echo "Hotovo. Aplikace běží na $BASE_URL"
echo "Přihlaste se heslem, které jste zvolili. Další krok: stránka Nápověda (příprava Google OAuth)."
