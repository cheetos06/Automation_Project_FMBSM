#!/usr/bin/env bash
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Run this script with sudo." >&2
  exit 1
fi

if [[ -n "${1:-}" && -f "$1" ]]; then
  public_key="$(tr -d '\r\n' < "$1")"
else
  public_key="${1:-${TOKEN_ARTIFACT_SFTP_PUBLIC_KEY:-}}"
fi
key_type="${public_key%% *}"
key_body="${public_key#* }"
key_body="${key_body%% *}"
if [[ "$key_type" != "ssh-ed25519" || ! "$key_body" =~ ^[A-Za-z0-9+/=]+$ ]]; then
  echo "Pass the GitHub Actions artifact uploader's ssh-ed25519 public key." >&2
  exit 1
fi

if ! id fmbsm-artifacts >/dev/null 2>&1; then
  useradd --no-create-home --home-dir /upload --shell /usr/sbin/nologin fmbsm-artifacts
fi
install -d -o root -g root -m 0755 /srv/fmbsm-artifacts
install -d -o fmbsm-artifacts -g fmbsm-artifacts -m 0755 /srv/fmbsm-artifacts/upload
install -d -o root -g root -m 0755 /etc/ssh/authorized_keys
printf '%s\n' "$public_key" > /etc/ssh/authorized_keys/fmbsm-artifacts
chown root:root /etc/ssh/authorized_keys/fmbsm-artifacts
chmod 0644 /etc/ssh/authorized_keys/fmbsm-artifacts

printf '%s\n' \
  'Match User fmbsm-artifacts' \
  '    AuthorizedKeysFile /etc/ssh/authorized_keys/%u' \
  '    ChrootDirectory /srv/fmbsm-artifacts' \
  '    ForceCommand internal-sftp -d /upload -u 022' \
  '    PasswordAuthentication no' \
  '    KbdInteractiveAuthentication no' \
  '    AllowAgentForwarding no' \
  '    AllowTcpForwarding no' \
  '    X11Forwarding no' \
  '    PermitTunnel no' > /etc/ssh/sshd_config.d/60-fmbsm-artifacts.conf

sshd -t
systemctl reload ssh
echo "Restricted artifact SFTP account configured."
