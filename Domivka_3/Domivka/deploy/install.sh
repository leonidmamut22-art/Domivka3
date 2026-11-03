#!/usr/bin/env bash
set -euo pipefail
[[ $(id -u) == 0 ]] || { echo 'Run with sudo on the VM.' >&2; exit 1; }
DOMIVKA_INSTANCE=main
DOMIVKA_HOST=''
DOMIVKA_PORT=8088
if [[ $# -gt 0 ]]; then DOMIVKA_INSTANCE="$1"; fi
if [[ $# -gt 1 ]]; then DOMIVKA_HOST="$2"; fi
if [[ $# -gt 2 ]]; then DOMIVKA_PORT="$3"; fi
[[ "$DOMIVKA_INSTANCE" =~ ^[a-z0-9][a-z0-9-]{0,31}$ ]] || exit 2
[[ "$DOMIVKA_HOST" =~ ^[a-z0-9][a-z0-9.-]+[a-z0-9]$ && "$DOMIVKA_HOST" != *..* ]] || { echo 'Specify your own hostname as second argument.' >&2; exit 2; }
[[ "$DOMIVKA_PORT" =~ ^[0-9]{4,5}$ && "$DOMIVKA_PORT" -ge 1024 && "$DOMIVKA_PORT" -le 65535 ]] || exit 2
DOMIVKA_SOURCE=$(cd "$(dirname "$0")/.." && pwd)
DOMIVKA_TARGET="/opt/domivka/$DOMIVKA_INSTANCE"
DOMIVKA_ENV="/etc/domivka/$DOMIVKA_INSTANCE.env"
apt-get update
apt-get install -y python3 python3-venv python3-pip caddy rsync
id domivka >/dev/null 2>&1 || useradd --system --home /var/lib/domivka --shell /usr/sbin/nologin domivka
install -d -m 0755 "$DOMIVKA_TARGET" /etc/domivka /etc/caddy/sites
if [[ "$DOMIVKA_SOURCE" != "$DOMIVKA_TARGET" ]]; then
  rsync -a --exclude=.env --exclude=data --exclude=.venv --exclude=__pycache__ --exclude=tests --exclude=test-results "$DOMIVKA_SOURCE/" "$DOMIVKA_TARGET/"
fi
python3 -m venv "$DOMIVKA_TARGET/.venv"
"$DOMIVKA_TARGET/.venv/bin/pip" install -r "$DOMIVKA_TARGET/bot/requirements.txt"
if [[ ! -f "$DOMIVKA_ENV" ]]; then
  read -rsp 'BotFather token (hidden): ' DOMIVKA_TOKEN; echo
  [[ "$DOMIVKA_TOKEN" =~ ^[0-9]+:[A-Za-z0-9_-]+$ ]] || { echo 'Invalid token format.' >&2; exit 2; }
  read -rp 'Bot username without @: ' DOMIVKA_BOTNAME
  [[ "$DOMIVKA_BOTNAME" =~ ^[A-Za-z0-9_]+$ ]] || exit 2
  read -rp 'Owner Telegram numeric ID: ' DOMIVKA_OWNER
  [[ "$DOMIVKA_OWNER" =~ ^[1-9][0-9]+$ ]] || exit 2
  umask 077
  printf 'BOT_TOKEN=%s\nEXPECTED_BOT_USERNAME=%s\nOWNER_ID=%s\nADMIN_IDS=%s\nDEPLOYMENT_MODE=production\nWEBAPP=on\nWEBAPP_HOST=127.0.0.1\nWEBAPP_PORT=%s\nWEBAPP_URL=https://%s\nTUNNEL=off\nDATA_DIR=/var/lib/domivka/%s\nPAYMENT_MODE=off\nREQUIRE_APPROVAL=1\n' "$DOMIVKA_TOKEN" "$DOMIVKA_BOTNAME" "$DOMIVKA_OWNER" "$DOMIVKA_OWNER" "$DOMIVKA_PORT" "$DOMIVKA_HOST" "$DOMIVKA_INSTANCE" > "$DOMIVKA_ENV"
  unset DOMIVKA_TOKEN
else
  echo "Using existing protected configuration: $DOMIVKA_ENV"
  echo 'Confirm WEBAPP_URL and WEBAPP_PORT match this invocation before restarting.'
fi
chmod 0600 "$DOMIVKA_ENV"
install -m 0644 "$DOMIVKA_TARGET/deploy/domivka@.service" /etc/systemd/system/domivka@.service
cat > "/etc/caddy/sites/domivka-$DOMIVKA_INSTANCE.caddy" <<CADDY
$DOMIVKA_HOST {
    encode zstd gzip
    header X-Content-Type-Options nosniff
    header Referrer-Policy strict-origin-when-cross-origin
    reverse_proxy 127.0.0.1:$DOMIVKA_PORT
}
CADDY
grep -Fq 'import /etc/caddy/sites/*.caddy' /etc/caddy/Caddyfile || printf '\nimport /etc/caddy/sites/*.caddy\n' >> /etc/caddy/Caddyfile
caddy validate --config /etc/caddy/Caddyfile
systemctl daemon-reload
systemctl enable --now caddy
systemctl reload caddy
systemctl enable "domivka@$DOMIVKA_INSTANCE"
systemctl restart "domivka@$DOMIVKA_INSTANCE"
echo "Service installed. Check: systemctl status domivka@$DOMIVKA_INSTANCE"
echo "Logs: journalctl -u domivka@$DOMIVKA_INSTANCE -f"
echo "Health: https://$DOMIVKA_HOST/api/identity"
echo 'Only one polling process may use the same bot token. Stop the old local instance before live cutover.'
