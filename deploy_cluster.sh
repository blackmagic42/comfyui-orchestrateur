#!/usr/bin/env bash
# deploy_cluster.sh — Déploie ComfyUI + orchestrator sur N machines via SSH.
#
# Pré-requis :
#   - Tous les hosts cibles doivent être joignables via SSH (clé dans authorized_keys)
#   - Tous doivent avoir python + git + nvidia-smi
#   - Stockage partagé NFS/SMB monté au MÊME path sur tous les hosts (recommandé)
#
# Architecture cible :
#
#                ┌─── dg1 (primary) : ComfyUI + orchestrator + DOWNLOADS
#                │     models/ → /mnt/cluster_models  (NFS mount)
#   You ─────►   ├─── dg2 (worker)  : ComfyUI + orchestrator (read-only)
#   (laptop)     │     models/ → /mnt/cluster_models
#                ├─── dg3 (worker)  : same
#                └─── dg4 (worker)  : same
#
# Usage :
#   bash deploy_cluster.sh \
#       --hosts "dg1@dg1 dg2@dg2 dg3@dg3 dg4@dg4" \
#       --primary "dg1@dg1" \
#       --shared-models /mnt/cluster_models \
#       [--install-path /home/dgX/comfyui] \
#       [--orchestrator-port 9000] \
#       [--budget 1500]

set -euo pipefail

# ── Subcommand routing ─────────────────────────────────────────────────────
SUBCMD="${1:-deploy}"
case "$SUBCMD" in
    deploy|parallel-download|setup-pool|pool-status) shift ;;
    -h|--help|help)
        sed -n '2,/^$/p' "$0"
        echo ""
        echo "Sub-commands:"
        echo "  deploy             (default) install ComfyUI + orchestrator on each host"
        echo "  parallel-download  Each host downloads its shard of the manifest"
        echo "  setup-pool         Configure NFS cross-mounts + symlink farm so each DG"
        echo "                     keeps 1/N locally but sees the full catalog"
        echo "  pool-status        Show pool occupancy per host"
        exit 0
        ;;
    *) ;;  # default to deploy
esac

HOSTS=""
PRIMARY=""
SHARED_MODELS=""
INSTALL_PATH=""
ORCH_PORT=9000
BUDGET=1500
MAX_AGE_YEARS=2
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PARALLEL=0
DRY_RUN=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --hosts)            HOSTS="$2"; shift 2 ;;
        --primary)          PRIMARY="$2"; shift 2 ;;
        --shared-models)    SHARED_MODELS="$2"; shift 2 ;;
        --install-path)     INSTALL_PATH="$2"; shift 2 ;;
        --orchestrator-port) ORCH_PORT="$2"; shift 2 ;;
        --budget)           BUDGET="$2"; shift 2 ;;
        --max-age-years)    MAX_AGE_YEARS="$2"; shift 2 ;;
        --parallel)         PARALLEL=1; shift ;;
        --dry-run)          DRY_RUN=1; shift ;;
        -h|--help)
            sed -n '2,/^$/p' "$0"
            exit 0
            ;;
        *) echo "Unknown arg: $1" >&2; exit 1 ;;
    esac
done

[[ -z "$HOSTS" ]] && { echo "ERROR: --hosts required (ex: 'dg1@dg1 dg2@dg2')" >&2; exit 1; }

ok()   { echo "  ✓ $*"; }
warn() { echo "  ⚠ $*" >&2; }
fail() { echo "  ✗ $*" >&2; exit 1; }
log()  { echo "[$(date +%H:%M:%S)] $*"; }

# ── Build the remote command per host ──────────────────────────────────────
build_remote_cmd() {
    local host="$1"
    local is_primary="$2"

    local args=()
    [[ -n "$INSTALL_PATH" ]]    && args+=("--install-path" "$INSTALL_PATH")
    [[ -n "$SHARED_MODELS" ]]   && args+=("--shared-models" "$SHARED_MODELS")
    args+=("--orchestrator-port" "$ORCH_PORT")
    args+=("--budget" "$BUDGET")
    args+=("--max-age-years" "$MAX_AGE_YEARS")
    [[ "$is_primary" == "1" ]]  && args+=("--primary")
    # Workers don't download models locally; they share the NFS mount
    [[ "$is_primary" != "1" ]]  && args+=("--skip-models")

    printf 'bash ~/install_comfyui.sh'
    for a in "${args[@]}"; do
        printf ' %q' "$a"
    done
}

# ── Deploy to a single host ────────────────────────────────────────────────
deploy_one() {
    local host="$1"
    local role="$2"

    log "═══ $host ($role) ═══"

    if [[ $DRY_RUN -eq 1 ]]; then
        log "[dry-run] would rsync scripts/ to $host:~"
        log "[dry-run] would run on $host: $(build_remote_cmd "$host" "$([[ "$role" == "primary" ]] && echo 1 || echo 0)")"
        return 0
    fi

    # 1. Ship the scripts/ directory + (optionally) the comfyui-workflow-manager extension
    log "Sync scripts to $host..."
    if ! rsync -az --delete \
            --exclude '__pycache__' --exclude '.catalog_state' \
            "$SCRIPT_DIR/" "${host}:~/.comfyui-scripts/"; then
        warn "rsync failed for $host"
        return 1
    fi

    # Also ship the workflow-manager extension if available
    WM_SRC="${ROOT_DIR}/ComfyUI/custom_nodes/comfyui-workflow-manager"
    if [[ -d "$WM_SRC" ]]; then
        rsync -az --exclude '__pycache__' "$WM_SRC/" \
              "${host}:~/.comfyui-workflow-manager/" 2>/dev/null || true
    fi

    # 2. Place install_comfyui.sh in $HOME for easy invocation
    ssh "$host" "cp ~/.comfyui-scripts/install_comfyui.sh ~/install_comfyui.sh && chmod +x ~/install_comfyui.sh"

    # 3. Run the install
    local is_primary=$([[ "$role" == "primary" ]] && echo "1" || echo "0")
    local cmd
    cmd=$(build_remote_cmd "$host" "$is_primary")
    log "Running on $host: $cmd"
    if ssh -t "$host" "$cmd"; then
        ok "$host install OK"
    else
        warn "$host install FAILED"
        return 1
    fi
}

# ── Main loop ────────────────────────────────────────────────────────────────
log "═══ ComfyUI cluster deployment ═══"
log "  Hosts          : $HOSTS"
log "  Primary        : ${PRIMARY:-<none, all hosts download independently>}"
log "  Shared models  : ${SHARED_MODELS:-<none, each host has local copy>}"
log "  Orchestrator   : :$ORCH_PORT"
log "  Budget         : $BUDGET GB"
log "  Parallel       : $PARALLEL"
log ""

if [[ -n "$SHARED_MODELS" && -z "$PRIMARY" ]]; then
    warn "Avec --shared-models, recommandé de désigner un --primary qui télécharge."
    warn "Sans primary, tous les hosts vont essayer de télécharger en même temps sur le même mount."
fi

failures=0
pids=()
for h in $HOSTS; do
    role="worker"
    if [[ "$h" == "$PRIMARY" ]]; then
        role="primary"
    fi

    if [[ $PARALLEL -eq 1 ]]; then
        deploy_one "$h" "$role" &
        pids+=($!)
    else
        if ! deploy_one "$h" "$role"; then
            failures=$((failures + 1))
        fi
    fi
done

if [[ $PARALLEL -eq 1 ]]; then
    log "Waiting for parallel deployments..."
    for pid in "${pids[@]}"; do
        wait "$pid" || failures=$((failures + 1))
    done
fi

# ── Parallel-download mode ─────────────────────────────────────────────────
# Each host runs `comfyui_catalog.py download-shard --shard N --total M` so the
# download_list is split across workers. All write to the SAME shared NFS
# mount → assembled catalog at the end.

run_parallel_download() {
    local hosts_arr=( $HOSTS )
    local total=${#hosts_arr[@]}

    if [[ -z "$PRIMARY" ]]; then
        warn "Pas de --primary spécifié. On utilise le premier host comme source du manifest."
        PRIMARY="${hosts_arr[0]}"
    fi

    log ""
    log "═══ Parallel download — $total workers ═══"
    log ""
    log "  Étape 1 : récupère le manifest depuis le primary ($PRIMARY)"

    # Pull download_list.json from primary
    local local_list="/tmp/cluster_download_list_$$.json"
    if ! scp "${PRIMARY}:.comfyui-scripts/../.catalog_state/download_list.json" "$local_list" 2>/dev/null; then
        # Fallback path
        if ! scp "${PRIMARY}:.catalog_state/download_list.json" "$local_list" 2>/dev/null; then
            fail "Impossible de récupérer download_list.json depuis $PRIMARY. Lance d'abord : ssh $PRIMARY 'bash ~/install_comfyui.sh ; ... ✨ Apply changes'"
        fi
    fi
    local total_size_gb=$(python -c "import json; d=json.load(open('$local_list')); print(round(sum(i.get('size',0) for i in d)/1024**3, 1))")
    log "  Manifest récupéré : $(wc -l <"$local_list" | tr -d ' ') lignes · ${total_size_gb} GB total"

    log ""
    log "  Étape 2 : push le manifest sur chaque worker"
    for h in "${hosts_arr[@]}"; do
        scp -q "$local_list" "${h}:.catalog_state/download_list.json" 2>/dev/null \
            || ssh "$h" "mkdir -p ~/.catalog_state && cat > ~/.catalog_state/download_list.json" < "$local_list"
        ok "  $h ← manifest"
    done
    rm -f "$local_list"

    log ""
    log "  Étape 3 : lance download-shard sur chaque worker (en parallèle)"
    log "  (les downloads vont sur ~/comfyui/models/ — qui peut être un mergerfs"
    log "   union vers /data/comfyui_local_shard si pool mode est setup)"
    local shard_pids=()
    local i=0
    for h in "${hosts_arr[@]}"; do
        log "    → $h : shard $i/$total"
        ssh "$h" "python ~/.comfyui-scripts/comfyui_catalog.py download-shard \
            --shard $i --total $total \
            --comfyui-path ~/comfyui \
            --api http://127.0.0.1:8188" \
            > "/tmp/dl_${h//[^a-zA-Z0-9]/_}.log" 2>&1 &
        shard_pids+=($!)
        i=$((i + 1))
    done

    log ""
    log "  Attente de tous les workers..."
    local total_failed=0
    for pid in "${shard_pids[@]}"; do
        wait "$pid" || total_failed=$((total_failed + 1))
    done

    log ""
    log "═══ Parallel download terminé ═══"
    log "  Workers : $total · Failed : $total_failed"
    log ""
    log "  Logs :"
    for h in "${hosts_arr[@]}"; do
        log "    /tmp/dl_${h//[^a-zA-Z0-9]/_}.log"
    done
    log ""
    log "  Vérif sur le NFS partagé (depuis le primary) :"
    log "    ssh $PRIMARY 'du -sh /mnt/cluster_models/'"
}

if [[ "$SUBCMD" == "parallel-download" ]]; then
    [[ -z "$HOSTS" ]] && fail "--hosts required"
    run_parallel_download
    exit 0
fi

# ── Pool mode (cross-mount + symlinks) ─────────────────────────────────────
# Architecture :
#   Chaque DGX :
#     - garde son shard local dans /data/comfyui_local_shard/
#     - exporte ce dossier en NFS pour les autres
#     - mount les 3 autres dans /mnt/peer_dg{0,1,2,3}/ (sauf soi)
#     - construit un symlink farm dans ~/comfyui/models/ qui pointe :
#         * sur les fichiers locaux quand owner == self
#         * sur les NFS mounts des peers sinon
#
# Ainsi chaque DGX voit l'intégralité du catalogue (~1.5 TB) tout en n'ayant
# que ~400 GB sur disque local (sur 4 DGX → 4 × 400 GB = 1.6 TB total).

run_setup_pool() {
    local hosts_arr=( $HOSTS )
    local total=${#hosts_arr[@]}
    local local_shard_dir="${POOL_LOCAL_DIR:-/data/comfyui_local_shard}"
    local peer_mount_pattern="${POOL_PEER_MOUNT:-/mnt/peer_dg}"
    local mode="${POOL_MODE:-mergerfs}"   # mergerfs (default) or symlink (fallback)

    log ""
    log "═══ Setup pool — mode: $mode ═══"
    log "  Hosts            : $HOSTS"
    log "  Local shard dir  : $local_shard_dir"
    log "  Peer mount path  : ${peer_mount_pattern}<shard>"
    log ""
    log "  Architecture :"
    log "    chaque DGX  - stocke 1/$total sur disque local ($local_shard_dir)"
    log "                - exporte ce dossier en NFS"
    log "                - monte les $((total - 1)) peers à ${peer_mount_pattern}<N>"
    log "                - voit le catalogue complet via $mode"
    log ""

    # ── Step 1 : NFS exports ─────────────────────────────────────────────────
    log "Step 1/4 : NFS exports + dirs"
    local i=0
    for h in "${hosts_arr[@]}"; do
        log "  → $h : exporte $local_shard_dir (shard $i)"
        ssh "$h" "
            sudo apt-get install -y nfs-kernel-server >/dev/null 2>&1 || true
            sudo mkdir -p '$local_shard_dir'
            sudo chown \$USER:\$USER '$local_shard_dir'
            if ! grep -q '$local_shard_dir' /etc/exports 2>/dev/null; then
                echo '$local_shard_dir ${POOL_SUBNET:-192.168.0.0/16}(rw,sync,no_subtree_check,no_root_squash,fsid=$i)' | sudo tee -a /etc/exports >/dev/null
                sudo exportfs -ra
            fi
            sudo systemctl enable --now nfs-kernel-server 2>/dev/null || sudo systemctl enable --now nfs-server 2>/dev/null || true
        " 2>/dev/null || warn "  $h : NFS export setup partiel (admin requis)"
        i=$((i + 1))
    done

    # ── Step 2 : Cross-mount peers ───────────────────────────────────────────
    log ""
    log "Step 2/4 : Mount cross-peer NFS"
    local self_idx=0
    for self_h in "${hosts_arr[@]}"; do
        for peer_idx in $(seq 0 $((total - 1))); do
            [[ $peer_idx -eq $self_idx ]] && continue
            local peer_h="${hosts_arr[$peer_idx]}"
            local peer_host="${peer_h##*@}"
            local mp="${peer_mount_pattern}${peer_idx}"
            ssh "$self_h" "
                sudo apt-get install -y nfs-common >/dev/null 2>&1 || true
                sudo mkdir -p '$mp'
                mountpoint -q '$mp' || sudo mount -t nfs '${peer_host}:$local_shard_dir' '$mp' 2>/dev/null
                if ! grep -q '$mp' /etc/fstab 2>/dev/null; then
                    echo '${peer_host}:$local_shard_dir $mp nfs defaults,_netdev 0 0' | sudo tee -a /etc/fstab >/dev/null
                fi
            " >/dev/null 2>&1 || warn "    ✗ $self_h ← $peer_host:$local_shard_dir"
        done
        log "  ✓ $self_h monte $((total - 1)) peers"
        self_idx=$((self_idx + 1))
    done

    # ── Step 3 : Build the union view at ComfyUI/models ──────────────────────
    log ""
    log "Step 3/4 : Build union view (mode=$mode)"
    self_idx=0
    for self_h in "${hosts_arr[@]}"; do
        # Comma-separated list of branches : self-shard first (writable), then peers
        local branches="$local_shard_dir"
        for peer_idx in $(seq 0 $((total - 1))); do
            [[ $peer_idx -eq $self_idx ]] && continue
            branches="${branches}:${peer_mount_pattern}${peer_idx}"
        done

        if [[ "$mode" == "mergerfs" ]]; then
            ssh "$self_h" "
                sudo apt-get install -y mergerfs >/dev/null 2>&1 || {
                    echo 'mergerfs install failed, falling back to symlink' >&2; exit 42;
                }
                # Backup existing models/ if regular dir
                if [[ -d ~/comfyui/models && ! -L ~/comfyui/models ]] && ! mountpoint -q ~/comfyui/models; then
                    mv ~/comfyui/models ~/comfyui/models.local-backup-\$(date +%s) || true
                fi
                mkdir -p ~/comfyui/models
                if ! mountpoint -q ~/comfyui/models; then
                    sudo mergerfs -o cache.files=auto-full,category.create=ff,minfreespace=1G \
                        '$branches' ~/comfyui/models
                fi
                # Persist via /etc/fstab
                FSTAB_LINE=\"$branches ~/comfyui/models fuse.mergerfs cache.files=auto-full,category.create=ff,minfreespace=1G,_netdev,allow_other 0 0\"
                if ! grep -q 'fuse.mergerfs.*comfyui/models' /etc/fstab 2>/dev/null; then
                    echo \"\$FSTAB_LINE\" | sudo tee -a /etc/fstab >/dev/null
                fi
            "
            local rc=$?
            if [[ $rc -eq 42 ]]; then
                # mergerfs unavailable, fallback to symlink
                warn "  $self_h : mergerfs absent, fallback symlink"
                ssh "$self_h" "python ~/.comfyui-scripts/comfyui_catalog.py pool-build \
                    --self $self_idx --total $total \
                    --peer-mount '${peer_mount_pattern}{shard}' \
                    --comfyui-path ~/comfyui" || warn "    symlink farm failed"
            else
                log "  ✓ $self_h : mergerfs union mounted"
            fi
        else
            # Symlink-only mode
            ssh "$self_h" "python ~/.comfyui-scripts/comfyui_catalog.py pool-build \
                --self $self_idx --total $total \
                --peer-mount '${peer_mount_pattern}{shard}' \
                --comfyui-path ~/comfyui" || warn "    symlink farm failed"
            log "  ✓ $self_h : symlink farm built"
        fi
        self_idx=$((self_idx + 1))
    done

    # ── Step 4 : verify ──────────────────────────────────────────────────────
    log ""
    log "Step 4/4 : Vérification"
    for h in "${hosts_arr[@]}"; do
        local n=$(ssh "$h" "ls ~/comfyui/models 2>/dev/null | wc -l" 2>/dev/null || echo "?")
        log "  $h : $n entrées dans ~/comfyui/models/"
    done

    log ""
    log "═══ Pool setup terminé ═══"
    log ""
    log "Lance le download parallèle :"
    log "  bash $(basename "$0") parallel-download \\"
    log "      --hosts \"$HOSTS\" --primary \"${hosts_arr[0]}\""
}

run_pool_status() {
    local hosts_arr=( $HOSTS )
    local local_shard_dir="${POOL_LOCAL_DIR:-/data/comfyui_local_shard}"

    log ""
    log "═══ Pool status ═══"
    printf "%-20s %-15s %-15s %s\n" "HOST" "LOCAL_SHARD" "MODELS_VIEW" "FILES"
    echo "──────────────────────────────────────────────────────────────────"
    for h in "${hosts_arr[@]}"; do
        local local_size=$(ssh "$h" "du -sh '$local_shard_dir' 2>/dev/null | cut -f1" 2>/dev/null || echo "?")
        local view_size=$(ssh "$h" "du -sLh ~/comfyui/models 2>/dev/null | cut -f1 | tail -1" 2>/dev/null || echo "?")
        local files=$(ssh "$h" "find ~/comfyui/models -name '*.safetensors' -o -name '*.gguf' 2>/dev/null | wc -l" 2>/dev/null || echo "?")
        printf "%-20s %-15s %-15s %s\n" "$h" "$local_size" "$view_size" "$files"
    done
    echo ""
}

if [[ "$SUBCMD" == "setup-pool" ]]; then
    [[ -z "$HOSTS" ]] && fail "--hosts required"
    run_setup_pool
    exit 0
fi

if [[ "$SUBCMD" == "pool-status" ]]; then
    [[ -z "$HOSTS" ]] && fail "--hosts required"
    run_pool_status
    exit 0
fi

log ""
log "═══ Cluster summary ═══"
log "  Hosts deployed : $(echo $HOSTS | wc -w)"
log "  Failures       : $failures"
log ""
log "  Orchestrators :"
for h in $HOSTS; do
    target_host="${h##*@}"
    log "    http://${target_host}:${ORCH_PORT}/dashboard"
done
log ""
log "💡 Conseils :"
log "  • Pour parallèliser le download : bash $(basename "$0") parallel-download \\"
log "      --hosts \"$HOSTS\" --primary \"$PRIMARY\" --shared-models \"${SHARED_MODELS:-/mnt/cluster_models}\""
log "  • Sinon, utilise un seul dashboard (dg1) avec /api/instances pointant sur les autres."

exit $failures
