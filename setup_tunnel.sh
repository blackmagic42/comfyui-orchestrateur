#!/usr/bin/env bash
# setup_tunnel.sh — Reverse SSH tunnel : VPS ←──── ComfyUI machine local
#
# Architecture :
#
#     [Browser] → [VPS:443/nginx] → 127.0.0.1:9000 (sur le VPS)
#                      ▲
#                      │ reverse tunnel SSH (-R)
#                      │
#     [Local machine: orchestrator.py serve --port 9000]
#
# Le port 9000 du VPS est mappé au port 9000 local via SSH.
# nginx sur le VPS termine le TLS et proxy_pass vers 127.0.0.1:9000.
#
# Usage :
#   bash setup_tunnel.sh start --vps user@vps.example.com [--remote-port 9000] [--local-port 9000]
#   bash setup_tunnel.sh stop
#   bash setup_tunnel.sh status
#

set -euo pipefail

CMD="${1:-help}"
shift || true

# ── Config defaults ─────────────────────────────────────────────────────────
VPS=""
REMOTE_PORT=9000
LOCAL_PORT=9000
SSH_KEY="${HOME}/.ssh/orchestrator_tunnel"
PIDFILE="/tmp/comfyui_tunnel.pid"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --vps)         VPS="$2"; shift 2 ;;
        --remote-port) REMOTE_PORT="$2"; shift 2 ;;
        --local-port)  LOCAL_PORT="$2"; shift 2 ;;
        --key)         SSH_KEY="$2"; shift 2 ;;
        *) echo "Unknown arg: $1" >&2; exit 1 ;;
    esac
done

case "$CMD" in
    keygen)
        if [[ ! -f "$SSH_KEY" ]]; then
            ssh-keygen -t ed25519 -f "$SSH_KEY" -N "" -C "comfyui-tunnel-$(hostname)"
            echo "✓ Clé générée : $SSH_KEY"
            echo ""
            echo "Pubkey à copier sur le VPS dans ~/.ssh/authorized_keys :"
            echo ""
            cat "${SSH_KEY}.pub"
            echo ""
            echo "Sur le VPS, restreins ce que peut faire cette clé en préfixant la"
            echo "ligne authorized_keys par :"
            echo "   command=\"echo Tunnel only\",no-pty,no-X11-forwarding,permitopen=\"127.0.0.1:${REMOTE_PORT}\" ssh-ed25519 ..."
        else
            echo "Clé déjà présente : $SSH_KEY"
        fi
        ;;
    start)
        [[ -z "$VPS" ]] && { echo "ERREUR: --vps requis (ex: user@host)" >&2; exit 1; }
        [[ ! -f "$SSH_KEY" ]] && { echo "ERREUR: clé absente. Lance: $0 keygen" >&2; exit 1; }

        if [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
            echo "Tunnel déjà actif (PID $(cat $PIDFILE))"
            exit 0
        fi

        echo "🔌 Démarrage tunnel : $VPS:${REMOTE_PORT} → 127.0.0.1:${LOCAL_PORT}"
        # autossh est plus robuste si dispo, sinon ssh natif
        if command -v autossh >/dev/null 2>&1; then
            autossh -M 0 -f -N \
                -o "ServerAliveInterval=30" -o "ServerAliveCountMax=3" \
                -o "ExitOnForwardFailure=yes" -o "StrictHostKeyChecking=accept-new" \
                -i "$SSH_KEY" \
                -R "${REMOTE_PORT}:127.0.0.1:${LOCAL_PORT}" \
                "$VPS"
            echo "$(pgrep -f "autossh.*${REMOTE_PORT}:127.0.0.1:${LOCAL_PORT}" | head -1)" > "$PIDFILE"
        else
            ssh -f -N \
                -o "ServerAliveInterval=30" -o "ServerAliveCountMax=3" \
                -o "ExitOnForwardFailure=yes" -o "StrictHostKeyChecking=accept-new" \
                -i "$SSH_KEY" \
                -R "${REMOTE_PORT}:127.0.0.1:${LOCAL_PORT}" \
                "$VPS"
            echo "$(pgrep -f "ssh.*-R.*${REMOTE_PORT}:127.0.0.1:${LOCAL_PORT}" | head -1)" > "$PIDFILE"
        fi
        echo "✓ Tunnel actif (PID $(cat $PIDFILE))"
        echo ""
        echo "Sur le VPS, configure nginx pour proxy_pass vers 127.0.0.1:${REMOTE_PORT}"
        echo "Exemple de bloc nginx :"
        echo ""
        cat <<EOF
server {
    listen 443 ssl http2;
    server_name comfyui.example.com;
    # ssl_certificate / ssl_certificate_key : Let's Encrypt
    location / {
        proxy_pass http://127.0.0.1:${REMOTE_PORT};
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 300s;
    }
}
EOF
        ;;
    stop)
        if [[ -f "$PIDFILE" ]]; then
            PID=$(cat "$PIDFILE")
            kill "$PID" 2>/dev/null || true
            rm "$PIDFILE"
            echo "✓ Tunnel arrêté"
        else
            echo "Aucun tunnel actif"
        fi
        ;;
    status)
        if [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
            echo "🟢 Tunnel actif (PID $(cat $PIDFILE))"
            netstat -tnp 2>/dev/null | grep -E "ssh|autossh" | head -5 || true
        else
            echo "🔴 Tunnel inactif"
        fi
        ;;
    *)
        cat <<HELP
Usage:
  $0 keygen                                    # Crée une paire de clés dédiée
  $0 start --vps user@vps.example.com          # Démarre le tunnel reverse
        [--remote-port 9000] [--local-port 9000]
        [--key ~/.ssh/orchestrator_tunnel]
  $0 stop                                      # Arrête le tunnel
  $0 status                                    # État du tunnel

Pré-requis sur le VPS :
  1. Copie de la pubkey dans ~/.ssh/authorized_keys
  2. Dans /etc/ssh/sshd_config :
       GatewayPorts no                      # garde 127.0.0.1 only (sécurisé)
       AllowTcpForwarding remote            # autorise -R
       ClientAliveInterval 30
  3. nginx avec proxy_pass vers 127.0.0.1:\$REMOTE_PORT
HELP
        ;;
esac
