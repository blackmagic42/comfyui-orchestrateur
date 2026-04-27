#!/usr/bin/env bash
# deploy_2dg.sh — Quick deploy ComfyUI sur dg2 + dg4 uniquement.
#
# Bootstrappe l'auth SSH par clé (1 seule utilisation des passwords),
# puis enchaîne deploy → setup-pool → parallel-download.
#
# Pré-requis :
#   - bash + ssh + rsync sur cette machine
#   - sshpass (auto-install si possible). Sur Git Bash Windows :
#       pacman -S sshpass    (MSYS2)
#       OU lance ce script depuis WSL Linux
#       OU ssh-copy-id manuellement avant
#   - Les 2 DG joignables via SSH (port 22 ouvert)
#
# Usage :
#   bash deploy_2dg.sh                  # full deploy (install + pool + download)
#   bash deploy_2dg.sh --skip-bootstrap # sauter le push de clé (déjà fait)
#   bash deploy_2dg.sh --bootstrap-only # juste push la clé puis sortir
#

set -euo pipefail

# ── Configuration des 2 hosts ──────────────────────────────────────────────
HOSTS=("dg2@dg2" "dg4@dg4")
PASSWORDS=("DG2dg2" "DG4dg4")

KEY="$HOME/.ssh/comfyui_dg_cluster"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Flags
SKIP_BOOTSTRAP=0
BOOTSTRAP_ONLY=0
SKIP_DEPLOY=0
SKIP_POOL=0
SKIP_DOWNLOAD=0
BUDGET=1500

while [[ $# -gt 0 ]]; do
    case "$1" in
        --skip-bootstrap) SKIP_BOOTSTRAP=1; shift ;;
        --bootstrap-only) BOOTSTRAP_ONLY=1; shift ;;
        --skip-deploy)    SKIP_DEPLOY=1; shift ;;
        --skip-pool)      SKIP_POOL=1; shift ;;
        --skip-download)  SKIP_DOWNLOAD=1; shift ;;
        --budget)         BUDGET="$2"; shift 2 ;;
        -h|--help)
            sed -n '2,/^$/p' "$0"; exit 0 ;;
        *) echo "Unknown arg: $1" >&2; exit 1 ;;
    esac
done

log()  { echo "[$(date +%H:%M:%S)] $*"; }
ok()   { echo "  ✓ $*"; }
warn() { echo "  ⚠ $*" >&2; }
fail() { echo "  ✗ $*" >&2; exit 1; }

log "═══ Deploy 2 DGX (dg2 + dg4) ═══"
log "  Hosts  : ${HOSTS[*]}"
log "  Key    : $KEY"
log "  Budget : $BUDGET GB"
log ""

# ── 1. Generate local SSH key dédiée ──────────────────────────────────────
if [[ ! -f "$KEY" ]]; then
    log "Génération de la clé SSH dédiée…"
    ssh-keygen -t ed25519 -f "$KEY" -N "" -C "comfyui_dg_cluster_$(date +%Y%m%d)"
    ok "Clé créée : $KEY"
fi

# ── 2. Bootstrap : push la clé sur chaque DG via password (1 seule fois) ──
if [[ $SKIP_BOOTSTRAP -eq 0 ]]; then
    log ""
    log "Push de la clé SSH sur les 2 DGX (auth par mot de passe une seule fois)…"

    if ! command -v sshpass >/dev/null 2>&1; then
        warn "sshpass introuvable — installation manuelle :"
        echo "    Linux   : sudo apt install sshpass"
        echo "    Mac     : brew install hudochenkov/sshpass/sshpass"
        echo "    MSYS2   : pacman -S sshpass"
        echo ""
        echo "  Ou alternative : copie la pubkey à la main :"
        echo "    cat $KEY.pub"
        echo "    puis sur chaque DG :"
        for h in "${HOSTS[@]}"; do
            echo "      ssh $h 'mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys'"
        done
        fail "Sans sshpass, push manuel requis. Relance avec --skip-bootstrap après."
    fi

    for i in "${!HOSTS[@]}"; do
        local_h="${HOSTS[$i]}"
        local_pw="${PASSWORDS[$i]}"
        log "  Push → $local_h"
        # Use ssh-copy-id (idempotent) with sshpass for the password
        if SSHPASS="$local_pw" sshpass -e ssh-copy-id \
              -f -i "$KEY.pub" \
              -o StrictHostKeyChecking=accept-new \
              -o UserKnownHostsFile="$HOME/.ssh/known_hosts" \
              "$local_h" </dev/null 2>&1 | grep -v "WARNING:" ; then
            ok "  $local_h : clé installée"
        else
            warn "  $local_h : push échoué (vérifie le password ou le réseau)"
        fi
    done
fi

# ── 3. Configure ~/.ssh/config pour utiliser la clé dédiée ───────────────
mkdir -p "$HOME/.ssh"
SSH_CONFIG="$HOME/.ssh/config"
touch "$SSH_CONFIG"

for h in "${HOSTS[@]}"; do
    user_part="${h%%@*}"
    host_part="${h##*@}"
    if ! grep -q "^Host $host_part$" "$SSH_CONFIG" 2>/dev/null; then
        cat >> "$SSH_CONFIG" <<EOF

Host $host_part
    HostName $host_part
    User $user_part
    IdentityFile $KEY
    StrictHostKeyChecking accept-new
    UserKnownHostsFile $HOME/.ssh/known_hosts
EOF
        ok "  ~/.ssh/config : entrée ajoutée pour $host_part"
    fi
done

# ── 4. Vérifier que la clé fonctionne sur les 2 hosts ────────────────────
log ""
log "Vérification SSH sans password…"
for h in "${HOSTS[@]}"; do
    if ssh -o BatchMode=yes -o ConnectTimeout=5 "$h" "echo OK" 2>/dev/null | grep -q OK; then
        ok "  $h ✓"
    else
        warn "  $h ✗ (la clé n'a pas été correctement installée)"
    fi
done

if [[ $BOOTSTRAP_ONLY -eq 1 ]]; then
    log ""
    log "✅ Bootstrap terminé. Re-lance sans --bootstrap-only pour le déploiement complet."
    exit 0
fi

HOSTS_STR="${HOSTS[*]}"
PRIMARY="${HOSTS[0]}"

# ── 5. deploy_cluster.sh deploy ───────────────────────────────────────────
if [[ $SKIP_DEPLOY -eq 0 ]]; then
    log ""
    log "═══ Étape 1/3 : Install ComfyUI sur ${HOSTS_STR} ═══"
    bash "$SCRIPT_DIR/deploy_cluster.sh" \
        --hosts "$HOSTS_STR" \
        --primary "$PRIMARY" \
        --budget "$BUDGET" \
        --parallel
fi

# ── 6. setup-pool ─────────────────────────────────────────────────────────
if [[ $SKIP_POOL -eq 0 ]]; then
    log ""
    log "═══ Étape 2/3 : Setup pool (NFS + mergerfs) ═══"
    log "  ⚠ Cette étape requiert sudo sur les DG (pour NFS exports/mounts)"
    bash "$SCRIPT_DIR/deploy_cluster.sh" setup-pool \
        --hosts "$HOSTS_STR"
fi

# ── 7. parallel-download ──────────────────────────────────────────────────
if [[ $SKIP_DOWNLOAD -eq 0 ]]; then
    log ""
    log "═══ Étape 3/3 : Build manifest + parallel download ═══"

    # First, build the manifest on the primary
    log "  Build manifest sur $PRIMARY (budget=$BUDGET GB)..."
    ssh "$PRIMARY" "python ~/.comfyui-scripts/comfyui_catalog.py build --budget $BUDGET" || \
        warn "Build manifest a échoué — vérifie via le dashboard"

    # Then dispatch the parallel download
    log "  Distribute le download sur les 2 DG (chacun ~$(( BUDGET / 2 )) GB)..."
    bash "$SCRIPT_DIR/deploy_cluster.sh" parallel-download \
        --hosts "$HOSTS_STR" \
        --primary "$PRIMARY"
fi

# ── 8. Résumé ────────────────────────────────────────────────────────────
log ""
log "═══ Déploiement terminé ═══"
log ""
log "🎛 Dashboards :"
for h in "${HOSTS[@]}"; do
    target="${h##*@}"
    log "    http://$target:9000/dashboard"
done

# Try to fetch the bearer token from primary
TOKEN=$(ssh "$PRIMARY" 'cat ~/.catalog_state/auth_token 2>/dev/null' 2>/dev/null || echo "")
if [[ -n "$TOKEN" ]]; then
    log "🔑 Token (paste in dashboard) : $TOKEN"
fi

log ""
log "Vérification du pool :"
log "    bash $(basename "$0" .sh)... pool-status"
log ""
log "Status :"
bash "$SCRIPT_DIR/deploy_cluster.sh" pool-status --hosts "$HOSTS_STR" 2>/dev/null || true
