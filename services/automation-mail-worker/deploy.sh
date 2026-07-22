#!/usr/bin/env bash
set -euo pipefail

SERVER_HOST="${SERVER_HOST:-35.180.210.11}"
SERVER_USER="${SERVER_USER:-ubuntu}"
REMOTE_DIR="${REMOTE_DIR:-/opt/fmbsm-automation}"

DEFAULT_KEY=""
for candidate in \
  "/c/Users/Anas.nmili/Desktop/AWS/LightsailDefaultKey-eu-west-3.pem" \
  "/mnt/c/Users/Anas.nmili/Desktop/AWS/LightsailDefaultKey-eu-west-3.pem" \
  "C:/Users/Anas.nmili/Desktop/AWS/LightsailDefaultKey-eu-west-3.pem"; do
  if [[ -f "$candidate" ]]; then
    DEFAULT_KEY="$candidate"
    break
  fi
done
SSH_KEY="${SSH_KEY:-$DEFAULT_KEY}"
if [[ -z "$SSH_KEY" || ! -f "$SSH_KEY" ]]; then
  echo "SSH key not found. Set SSH_KEY to the Lightsail .pem path." >&2
  exit 1
fi
if [[ ! -f .env ]]; then
  echo "Missing .env. Copy .env.example and set the Gmail app password." >&2
  exit 1
fi
if grep -q "replace-with-gmail-app-password" .env; then
  echo ".env still contains the placeholder Gmail password." >&2
  exit 1
fi

archive="$(mktemp -t fmbsm-automation.XXXXXX.tar.gz)"
trap 'rm -f "$archive"' EXIT
tar \
  --exclude=.env \
  --exclude=.venv \
  --exclude=data \
  --exclude='**/__pycache__' \
  --exclude='**/*.pyc' \
  -czf "$archive" .

scp -i "$SSH_KEY" "$archive" "${SERVER_USER}@${SERVER_HOST}:/tmp/fmbsm-automation.tar.gz"
scp -i "$SSH_KEY" .env "${SERVER_USER}@${SERVER_HOST}:/tmp/fmbsm-automation.env"

ssh -i "$SSH_KEY" "${SERVER_USER}@${SERVER_HOST}" bash -s -- "$REMOTE_DIR" "$SERVER_USER" "$SERVER_HOST" <<'REMOTE'
set -euo pipefail
REMOTE_DIR="$1"
SERVER_USER="$2"
SERVER_HOST="$3"

sudo mkdir -p "$REMOTE_DIR"
sudo chown "$SERVER_USER:$SERVER_USER" "$REMOTE_DIR"
old_upload_key=""
old_admin_key=""
if [[ -f "$REMOTE_DIR/.env" ]]; then
  old_upload_key="$(sed -n 's/^COPILOT_UPLOAD_KEY=//p' "$REMOTE_DIR/.env" | tail -1)"
  old_admin_key="$(sed -n 's/^TOKEN_ADMIN_KEY=//p' "$REMOTE_DIR/.env" | tail -1)"
fi
tar -xzf /tmp/fmbsm-automation.tar.gz -C "$REMOTE_DIR"
cp /tmp/fmbsm-automation.env "$REMOTE_DIR/.env"
# A copied dotenv file may legitimately omit its final newline. Add one before
# appending managed settings so two variable names can never be concatenated.
printf '\n' >> "$REMOTE_DIR/.env"

ensure_env() {
  local name="$1"
  local value="$2"
  if ! grep -q "^${name}=" "$REMOTE_DIR/.env"; then
    printf '%s=%s\n' "$name" "$value" >> "$REMOTE_DIR/.env"
  fi
}
incoming_upload_key="$(sed -n 's/^COPILOT_UPLOAD_KEY=//p' "$REMOTE_DIR/.env" | tail -1)"
if [[ ${#incoming_upload_key} -lt 32 || "$incoming_upload_key" == replace-* ]]; then
  sed -i '/^COPILOT_UPLOAD_KEY=/d' "$REMOTE_DIR/.env"
  if [[ ${#old_upload_key} -lt 32 || "$old_upload_key" == replace-* ]]; then
    old_upload_key="$(openssl rand -hex 32)"
  fi
  printf 'COPILOT_UPLOAD_KEY=%s\n' "$old_upload_key" >> "$REMOTE_DIR/.env"
fi
incoming_admin_key="$(sed -n 's/^TOKEN_ADMIN_KEY=//p' "$REMOTE_DIR/.env" | tail -1)"
if [[ ${#incoming_admin_key} -lt 32 || "$incoming_admin_key" == replace-* ]]; then
  sed -i '/^TOKEN_ADMIN_KEY=/d' "$REMOTE_DIR/.env"
  if [[ ${#old_admin_key} -lt 32 || "$old_admin_key" == replace-* ]]; then
    old_admin_key="$(openssl rand -hex 32)"
  fi
  printf 'TOKEN_ADMIN_KEY=%s\n' "$old_admin_key" >> "$REMOTE_DIR/.env"
fi
ensure_env BOT_DATA_DIR "$REMOTE_DIR/data/mail"
ensure_env COPILOT_DATA_DIR "$REMOTE_DIR/data/copilot"
ensure_env COPILOT_REGISTRY_DB "$REMOTE_DIR/data/copilot/registry.sqlite3"
ensure_env JOB_STATUS_DIR "$REMOTE_DIR/data/mail/state/job-status"
ensure_env TOKEN_API_HOST "0.0.0.0"
ensure_env TOKEN_API_PORT "443"
ensure_env TOKEN_API_CERT_FILE "$REMOTE_DIR/data/tls/server.crt"
ensure_env TOKEN_API_KEY_FILE "$REMOTE_DIR/data/tls/server.key"
ensure_env TOKEN_CLIENT_ARTIFACT_DIR "/srv/fmbsm-artifacts/upload/token-client"
ensure_env FS_SUBJECT_PREFIX "[fs-review]"
ensure_env FS_REVIEW_TIMEOUT_SECONDS "10800"
ensure_env EFFECTIF_SUBJECT_PREFIX "[optimda-effectif]"
ensure_env EFFECTIF_TIMEOUT_SECONDS "21600"
ensure_env MAX_RESULT_ATTACHMENT_BYTES "20971520"
ensure_env MAX_QUEUED_JOBS "50"
ensure_env MIN_FREE_DISK_BYTES "2147483648"
ensure_env QUEUE_RETRY_DELAY_SECONDS "30"
ensure_env QUEUE_DEFAULT_FS_SECONDS "900"
ensure_env QUEUE_DEFAULT_EFFECTIF_SECONDS "900"
ensure_env QUEUE_DEFAULT_SIGNATURE_SECONDS "300"
ensure_env SEND_RETRY_NOTIFICATIONS "true"
chmod 600 "$REMOTE_DIR/.env"

sudo apt-get update
sudo apt-get install -y \
  ca-certificates libgdiplus libglib2.0-0 libgomp1 libicu74 libssl-dev \
  openssl p7zip-full python3-pip python3-venv unzip wget

if ! sudo swapon --show=NAME --noheadings | grep -q .; then
  if [[ ! -f /swapfile ]]; then
    sudo fallocate -l 2G /swapfile
    sudo chmod 600 /swapfile
    sudo mkswap /swapfile >/dev/null
  fi
  sudo swapon /swapfile
  if ! grep -q '^/swapfile ' /etc/fstab; then
    echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
  fi
fi

if ! ldconfig -p | grep -q 'libssl.so.1.1'; then
  wget -q -O /tmp/libssl1.1.deb \
    https://archive.ubuntu.com/ubuntu/pool/main/o/openssl/libssl1.1_1.1.1f-1ubuntu2.24_amd64.deb
  echo '7cf39d70a639017d1dd7c8d36daa2258063608688e449fddf40ffdd46f992a78  /tmp/libssl1.1.deb' | sha256sum -c -
  sudo apt-get install -y /tmp/libssl1.1.deb
fi

mkdir -p \
  "$REMOTE_DIR/data/mail/logs" \
  "$REMOTE_DIR/data/mail/jobs" \
  "$REMOTE_DIR/data/mail/processed" \
  "$REMOTE_DIR/data/mail/failed" \
  "$REMOTE_DIR/data/mail/state/job-status" \
  "$REMOTE_DIR/data/copilot/accounts" \
  "$REMOTE_DIR/data/tls"

# One-time migration from the original mail-only deployment. Existing message
# markers prevent old unread mail from being processed twice after cutover.
LEGACY_DATA_DIR="/opt/fmbsm-email-bot"
if [[ -d "$LEGACY_DATA_DIR" && "$LEGACY_DATA_DIR" != "$REMOTE_DIR/data/mail" ]]; then
  for item in processed failed state; do
    if [[ -d "$LEGACY_DATA_DIR/$item" ]]; then
      cp -a -n "$LEGACY_DATA_DIR/$item/." "$REMOTE_DIR/data/mail/$item/"
    fi
  done
fi
if [[ ! -s "$REMOTE_DIR/data/tls/server.crt" || ! -s "$REMOTE_DIR/data/tls/server.key" ]]; then
  openssl req -x509 -newkey rsa:3072 -sha256 -nodes -days 3650 \
    -keyout "$REMOTE_DIR/data/tls/server.key" \
    -out "$REMOTE_DIR/data/tls/server.crt" \
    -subj "/CN=$SERVER_HOST" \
    -addext "subjectAltName=IP:$SERVER_HOST"
  chmod 600 "$REMOTE_DIR/data/tls/server.key"
  chmod 644 "$REMOTE_DIR/data/tls/server.crt"
fi

cd "$REMOTE_DIR"
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip wheel
python -m pip install -r requirements.txt
python -m compileall -q fmbsm_email_bot copilot_service copilot_runtime fs_review effectif_extract
deactivate

sudo cp systemd/fmbsm-token-api.service /etc/systemd/system/fmbsm-token-api.service
sudo cp systemd/fmbsm-token-api-http.service /etc/systemd/system/fmbsm-token-api-http.service
sudo cp systemd/fmbsm-email-bot.service /etc/systemd/system/fmbsm-email-bot.service
sudo systemctl daemon-reload
sudo systemctl enable fmbsm-token-api fmbsm-token-api-http fmbsm-email-bot
sudo systemctl restart fmbsm-token-api
sudo systemctl restart fmbsm-token-api-http
sleep 2
curl --fail --silent --show-error \
  --cacert "$REMOTE_DIR/data/tls/server.crt" \
  --resolve "$SERVER_HOST:443:127.0.0.1" \
  "https://$SERVER_HOST/health" >/dev/null
curl --fail --silent --show-error "http://127.0.0.1/health" >/dev/null
sudo systemctl restart fmbsm-email-bot
sudo systemctl --no-pager --full status fmbsm-token-api | sed -n '1,12p'
sudo systemctl --no-pager --full status fmbsm-token-api-http | sed -n '1,12p'
sudo systemctl --no-pager --full status fmbsm-email-bot | sed -n '1,12p'
REMOTE

echo "Deployment and local HTTPS smoke test completed."
echo "Live logs:"
echo "  ssh -i \"$SSH_KEY\" ${SERVER_USER}@${SERVER_HOST} 'sudo journalctl -u fmbsm-email-bot -u fmbsm-token-api -f'"
