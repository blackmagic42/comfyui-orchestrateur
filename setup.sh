#!/usr/bin/env bash
# setup.sh — Menu interactif unifié pour ComfyUI install / cluster.
#
# Couvre :
#   1. Install sur 1 machine (laptop, station, DGX isolé)
#   2. Déploiement cluster avec modèles partagés via NFS (1 primary télécharge)
#   3. Déploiement cluster en mode pool (chaque DG stocke 1/N, voit N/N)
#   4. Configuration du pool sur cluster déjà installé
#   5. Status du cluster
#   6. Stopper le cluster
#
# Délègue à install_comfyui.sh / deploy_cluster.sh — c'est juste l'UX.
#
# Usage :
#   bash setup.sh                  # menu interactif
#   bash setup.sh --single         # raccourci direct vers le mode single
#   bash setup.sh --cluster        # raccourci direct vers le mode cluster

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── Colors ────────────────────────────────────────────────────────────────────
if [[ -t 1 ]]; then
    BOLD=$'\033[1m'; DIM=$'\033[2m'; RESET=$'\033[0m'
    GREEN=$'\033[32m'; YELLOW=$'\033[33m'; BLUE=$'\033[34m'; CYAN=$'\033[36m'; RED=$'\033[31m'
else
    BOLD=""; DIM=""; RESET=""; GREEN=""; YELLOW=""; BLUE=""; CYAN=""; RED=""
fi

# ── UI helpers ────────────────────────────────────────────────────────────────
banner() {
    cat <<EOF
${CYAN}${BOLD}
  ╔═══════════════════════════════════════════════════════════════╗
  ║         ComfyUI — Installation & Cluster Manager              ║
  ║                  Interactive setup wizard                     ║
  ╚═══════════════════════════════════════════════════════════════╝
${RESET}
EOF
}

step()  { echo "${BLUE}${BOLD}▸${RESET} ${BOLD}$*${RESET}"; }
ok()    { echo "  ${GREEN}✓${RESET} $*"; }
warn()  { echo "  ${YELLOW}⚠${RESET} $*"; }
err()   { echo "  ${RED}✗${RESET} $*" >&2; }
hint()  { echo "  ${DIM}$*${RESET}"; }

# Ask a question with a default value. Usage: ask "Prompt" "default"
ask() {
    local prompt="$1" default="${2:-}" answer
    if [[ -n "$default" ]]; then
        read -r -p "${BOLD}? ${RESET}${prompt} ${DIM}[${default}]${RESET} " answer
        echo "${answer:-$default}"
    else
        read -r -p "${BOLD}? ${RESET}${prompt} " answer
        echo "$answer"
    fi
}

# Yes/No prompt; default Y if no second arg, N if "n". Returns 0 for yes.
confirm() {
    local prompt="$1" default="${2:-y}" answer
    local hint
    [[ "$default" == "y" ]] && hint="[Y/n]" || hint="[y/N]"
    while :; do
        read -r -p "${BOLD}? ${RESET}${prompt} ${DIM}${hint}${RESET} " answer
        answer="${answer:-$default}"
        case "$answer" in
            [yY]|[yY][eE][sS]) return 0 ;;
            [nN]|[nN][oO])     return 1 ;;
            *) warn "Réponds y ou n." ;;
        esac
    done
}

# Validate a positive integer. Echoes the value or exits.
validate_int() {
    local val="$1" label="$2"
    if ! [[ "$val" =~ ^[0-9]+$ ]] || [[ "$val" -le 0 ]]; then
        err "$label doit être un entier positif (reçu: '$val')"
        return 1
    fi
}

# ── Pré-vérifs ────────────────────────────────────────────────────────────────
check_prereqs_single() {
    step "Vérification des pré-requis locaux"
    local missing=()
    for cmd in python git bash; do
        command -v "$cmd" >/dev/null 2>&1 || missing+=("$cmd")
    done
    if [[ ${#missing[@]} -gt 0 ]]; then
        err "Outils manquants : ${missing[*]}"
        return 1
    fi
    ok "python, git, bash : OK"
    if command -v nvidia-smi >/dev/null 2>&1; then
        ok "nvidia-smi détecté"
    else
        warn "nvidia-smi absent — l'install va passer en mode CPU/MPS"
    fi
}

check_prereqs_cluster() {
    step "Vérification des pré-requis (poste de contrôle)"
    local missing=()
    for cmd in ssh rsync bash python; do
        command -v "$cmd" >/dev/null 2>&1 || missing+=("$cmd")
    done
    if [[ ${#missing[@]} -gt 0 ]]; then
        err "Outils manquants : ${missing[*]}"
        return 1
    fi
    ok "ssh, rsync, bash, python : OK"
}

# Vérifie la connectivité SSH vers chaque host listé
ssh_check_hosts() {
    local hosts=("$@")
    local failed=0
    step "Test de connectivité SSH"
    for h in "${hosts[@]}"; do
        if ssh -o BatchMode=yes -o ConnectTimeout=5 "$h" 'true' 2>/dev/null; then
            ok "$h"
        else
            err "$h : impossible de se connecter (clé SSH manquante ?)"
            failed=$((failed + 1))
        fi
    done
    [[ $failed -eq 0 ]]
}

# ── Modes ─────────────────────────────────────────────────────────────────────

mode_single() {
    banner
    step "Mode : ${BOLD}Installation sur cette machine${RESET}"
    echo ""
    hint "ComfyUI + custom nodes + workflows + orchestrateur seront installés localement."
    hint "Aucune connexion SSH ni configuration réseau partagée."
    echo ""

    check_prereqs_single || return 1

    local default_install="${HOME}/comfyui"
    local install_path budget max_age orch_port skip_models skip_workflows no_orch
    install_path=$(ask "Chemin d'installation" "$default_install")
    budget=$(ask "Budget de stockage en GB (modèles ≤ ce budget)" "1024")
    max_age=$(ask "Âge max des modèles en années" "2")
    orch_port=$(ask "Port de l'orchestrateur" "9000")
    validate_int "$budget" "budget" || return 1
    validate_int "$max_age" "max-age" || return 1
    validate_int "$orch_port" "port" || return 1

    skip_models="0"; skip_workflows="0"; no_orch="0"
    confirm "Télécharger les modèles maintenant ? (sinon tu choisiras le budget depuis le dashboard)" "n" || skip_models="1"
    confirm "Copier les 218 workflows de référence ?" "y" || skip_workflows="1"
    confirm "Lancer l'orchestrateur en fin d'install ?" "y" || no_orch="1"

    echo ""
    step "Récap"
    cat <<EOF
  Install path     : ${BOLD}${install_path}${RESET}
  Budget           : ${BOLD}${budget} GB${RESET}
  Max age (years)  : ${BOLD}${max_age}${RESET}
  Orchestrator     : ${BOLD}port ${orch_port}${RESET}
  Skip models DL   : $([[ "$skip_models" == "1" ]] && echo "${YELLOW}oui${RESET}" || echo "non")
  Skip workflows   : $([[ "$skip_workflows" == "1" ]] && echo "${YELLOW}oui${RESET}" || echo "non")
  Auto-launch UI   : $([[ "$no_orch" == "1" ]] && echo "${YELLOW}non${RESET}" || echo "${GREEN}oui${RESET}")
EOF
    echo ""
    confirm "Lancer l'installation ?" "y" || { warn "Annulé."; return 0; }

    local args=(
        --install-path "$install_path"
        --budget "$budget"
        --max-age-years "$max_age"
        --orchestrator-port "$orch_port"
    )
    [[ "$skip_models" == "1" ]] && args+=(--skip-models)
    [[ "$skip_workflows" == "1" ]] && args+=(--skip-workflows)
    [[ "$no_orch" == "1" ]] && args+=(--no-orchestrator)

    echo ""
    step "Lancement de install_comfyui.sh"
    bash "${SCRIPT_DIR}/install_comfyui.sh" "${args[@]}"
}

# Lit les hosts depuis le user (un par ligne, ligne vide pour terminer)
read_hosts() {
    local hosts=() line
    echo "${BOLD}Entre tes hosts (format: ${DIM}user@host${RESET}${BOLD}), un par ligne. Ligne vide = fin.${RESET}"
    hint "Exemple : dgx1@192.168.1.10"
    while :; do
        read -r -p "  → " line
        if [[ -z "$line" ]]; then
            [[ ${#hosts[@]} -eq 0 ]] && { warn "Au moins un host requis."; continue; }
            break
        fi
        hosts+=("$line")
    done
    printf '%s\n' "${hosts[@]}"
}

mode_cluster_shared() {
    banner
    step "Mode : ${BOLD}Cluster avec modèles partagés (NFS)${RESET}"
    echo ""
    hint "1 machine ${BOLD}primary${RESET} télécharge → les workers utilisent un NFS mount."
    hint "Idéal quand tu as déjà un NAS et un sous-réseau privé."
    echo ""

    check_prereqs_cluster || return 1

    local hosts=()
    mapfile -t hosts < <(read_hosts)
    local total=${#hosts[@]}

    echo ""
    step "Choix du primary"
    for i in "${!hosts[@]}"; do
        echo "  $((i+1)). ${hosts[$i]}"
    done
    local primary_idx primary
    primary_idx=$(ask "Quel host est le primary (numéro)" "1")
    if ! [[ "$primary_idx" =~ ^[0-9]+$ ]] || [[ "$primary_idx" -lt 1 ]] || [[ "$primary_idx" -gt $total ]]; then
        err "Index invalide"; return 1
    fi
    primary="${hosts[$((primary_idx-1))]}"

    local shared_path budget max_age orch_port parallel
    shared_path=$(ask "Chemin du mount NFS partagé (identique sur tous les hosts)" "/mnt/cluster_models")
    budget=$(ask "Budget de stockage (GB) — taille du catalogue" "1500")
    max_age=$(ask "Âge max des modèles (années)" "2")
    orch_port=$(ask "Port orchestrateur (sur chaque host)" "9000")
    validate_int "$budget" "budget" || return 1
    validate_int "$max_age" "max-age" || return 1
    validate_int "$orch_port" "port" || return 1

    parallel="0"
    confirm "Lancer les installs en parallèle (gain de temps mais logs entrelacés) ?" "y" && parallel="1"

    echo ""
    step "Récap"
    cat <<EOF
  Hosts (${total})       : ${hosts[*]}
  Primary          : ${BOLD}${primary}${RESET}
  Shared NFS path  : ${BOLD}${shared_path}${RESET}
  Budget           : ${BOLD}${budget} GB${RESET}
  Max age          : ${max_age} ans
  Orchestrator     : port ${orch_port}
  Parallel install : $([[ "$parallel" == "1" ]] && echo "oui" || echo "non")
EOF
    echo ""

    if confirm "Tester la connectivité SSH avant de lancer ?" "y"; then
        ssh_check_hosts "${hosts[@]}" || {
            confirm "Continuer malgré les erreurs ?" "n" || return 1
        }
    fi

    confirm "Lancer le déploiement ?" "y" || { warn "Annulé."; return 0; }

    local args=(
        --hosts "${hosts[*]}"
        --primary "$primary"
        --shared-models "$shared_path"
        --orchestrator-port "$orch_port"
        --budget "$budget"
        --max-age-years "$max_age"
    )
    [[ "$parallel" == "1" ]] && args+=(--parallel)

    echo ""
    step "Lancement de deploy_cluster.sh"
    bash "${SCRIPT_DIR}/deploy_cluster.sh" "${args[@]}"
}

mode_cluster_pool() {
    banner
    step "Mode : ${BOLD}Cluster en mode pool (chaque DG stocke 1/N, voit N/N)${RESET}"
    echo ""
    hint "Chaque host garde 1/N du catalogue localement et monte les autres via NFS."
    hint "Ex : 4 DGX × 1 TB → catalogue 1,5 TB partagé sans duplication."
    hint "Requiert mergerfs (auto-fallback symlink farm sinon)."
    echo ""

    check_prereqs_cluster || return 1

    local hosts=()
    mapfile -t hosts < <(read_hosts)
    local total=${#hosts[@]}
    if [[ $total -lt 2 ]]; then
        err "Le mode pool a besoin d'au moins 2 hosts."
        return 1
    fi

    echo ""
    step "Choix du primary (celui qui télécharge / construit le manifest)"
    for i in "${!hosts[@]}"; do
        echo "  $((i+1)). ${hosts[$i]}"
    done
    local primary_idx primary
    primary_idx=$(ask "Quel host est le primary (numéro)" "1")
    primary="${hosts[$((primary_idx-1))]}"

    local budget max_age install_first
    budget=$(ask "Budget total du catalogue (GB) — réparti entre les hosts" "1500")
    max_age=$(ask "Âge max des modèles (années)" "2")
    validate_int "$budget" "budget" || return 1
    validate_int "$max_age" "max-age" || return 1

    install_first="0"
    confirm "Installer ComfyUI sur les hosts d'abord (s'ils n'ont rien encore) ?" "y" && install_first="1"

    echo ""
    step "Récap"
    cat <<EOF
  Hosts (${total})       : ${hosts[*]}
  Primary          : ${BOLD}${primary}${RESET}
  Mode             : ${BOLD}pool (mergerfs)${RESET}
  Budget total     : ${BOLD}${budget} GB${RESET} (~$((budget / total)) GB par host)
  Install d'abord  : $([[ "$install_first" == "1" ]] && echo "oui" || echo "non")
EOF
    echo ""

    if confirm "Tester la connectivité SSH avant de lancer ?" "y"; then
        ssh_check_hosts "${hosts[@]}" || {
            confirm "Continuer malgré les erreurs ?" "n" || return 1
        }
    fi

    confirm "Lancer la séquence ?" "y" || { warn "Annulé."; return 0; }

    if [[ "$install_first" == "1" ]]; then
        echo ""
        step "Étape 1/3 — Install ComfyUI sur chaque host (parallèle)"
        bash "${SCRIPT_DIR}/deploy_cluster.sh" \
            --hosts "${hosts[*]}" \
            --primary "$primary" \
            --budget "$budget" \
            --max-age-years "$max_age" \
            --parallel
    fi

    echo ""
    step "Étape 2/3 — Setup pool (NFS cross-mount + mergerfs union)"
    bash "${SCRIPT_DIR}/deploy_cluster.sh" setup-pool --hosts "${hosts[*]}"

    echo ""
    if confirm "Lancer le download parallèle réparti maintenant ?" "y"; then
        step "Étape 3/3 — Download parallèle (chaque host prend 1/N)"
        bash "${SCRIPT_DIR}/deploy_cluster.sh" parallel-download \
            --hosts "${hosts[*]}" \
            --primary "$primary"
    else
        hint "Tu peux lancer plus tard via l'option 6 du menu, ou :"
        hint "  bash ${SCRIPT_DIR}/deploy_cluster.sh parallel-download --hosts \"${hosts[*]}\" --primary \"$primary\""
    fi
}

mode_pool_only() {
    banner
    step "Mode : ${BOLD}Configurer le pool sur cluster déjà installé${RESET}"
    echo ""
    hint "Suppose que ComfyUI est déjà installé sur chaque host."
    hint "Ne fait que monter les NFS croisés + mergerfs."
    echo ""

    local hosts=()
    mapfile -t hosts < <(read_hosts)

    echo ""
    confirm "Lancer le setup-pool ?" "y" || return 0
    bash "${SCRIPT_DIR}/deploy_cluster.sh" setup-pool --hosts "${hosts[*]}"
}

mode_status() {
    banner
    step "Mode : ${BOLD}Status du cluster${RESET}"
    echo ""

    local hosts=()
    mapfile -t hosts < <(read_hosts)

    bash "${SCRIPT_DIR}/deploy_cluster.sh" pool-status --hosts "${hosts[*]}"
}

mode_stop() {
    banner
    step "Mode : ${BOLD}Stopper le cluster${RESET}"
    echo ""
    hint "Tue les processus ComfyUI (port 8188) et orchestrateur (port 9000) sur chaque host."
    echo ""

    local hosts=()
    mapfile -t hosts < <(read_hosts)

    confirm "${RED}Confirmer l'arrêt sur tous ces hosts ?${RESET}" "n" || { warn "Annulé."; return 0; }

    for h in "${hosts[@]}"; do
        if ssh "$h" 'kill $(lsof -ti:9000) $(lsof -ti:8188) 2>/dev/null || true'; then
            ok "$h : processus stoppés"
        else
            warn "$h : impossible de joindre"
        fi
    done
}

mode_parallel_download() {
    banner
    step "Mode : ${BOLD}Download parallèle (cluster pool déjà setup)${RESET}"
    echo ""

    local hosts=()
    mapfile -t hosts < <(read_hosts)

    local primary_idx primary
    echo ""
    for i in "${!hosts[@]}"; do
        echo "  $((i+1)). ${hosts[$i]}"
    done
    primary_idx=$(ask "Quel host a déjà le manifest (primary, numéro)" "1")
    primary="${hosts[$((primary_idx-1))]}"

    confirm "Lancer le download réparti ?" "y" || return 0
    bash "${SCRIPT_DIR}/deploy_cluster.sh" parallel-download \
        --hosts "${hosts[*]}" --primary "$primary"
}

# ── Main menu ────────────────────────────────────────────────────────────────
main_menu() {
    while :; do
        banner
        cat <<EOF
${BOLD}Que veux-tu faire ?${RESET}

  ${GREEN}1)${RESET} Installer ComfyUI sur ${BOLD}cette machine${RESET} uniquement
  ${GREEN}2)${RESET} Déployer un ${BOLD}cluster${RESET} avec modèles partagés ${DIM}(1 primary + workers via NFS)${RESET}
  ${GREEN}3)${RESET} Déployer un ${BOLD}cluster en pool${RESET} ${DIM}(chaque DG stocke 1/N, voit N/N)${RESET}
  ${GREEN}4)${RESET} Configurer le ${BOLD}pool${RESET} ${DIM}(cluster déjà installé)${RESET}
  ${GREEN}5)${RESET} ${BOLD}Status${RESET} du cluster
  ${GREEN}6)${RESET} ${BOLD}Download parallèle${RESET} ${DIM}(pool déjà configuré)${RESET}
  ${GREEN}7)${RESET} ${BOLD}Stopper${RESET} le cluster
  ${GREEN}q)${RESET} Quitter

EOF
        local choice
        choice=$(ask "Choix" "")
        echo ""
        case "$choice" in
            1) mode_single ;;
            2) mode_cluster_shared ;;
            3) mode_cluster_pool ;;
            4) mode_pool_only ;;
            5) mode_status ;;
            6) mode_parallel_download ;;
            7) mode_stop ;;
            q|Q|quit|exit) exit 0 ;;
            *) warn "Choix invalide : '$choice'" ;;
        esac
        echo ""
        confirm "Retour au menu principal ?" "y" || break
    done
}

# ── CLI shortcuts ────────────────────────────────────────────────────────────
case "${1:-}" in
    --single)               mode_single ;;
    --cluster-shared)       mode_cluster_shared ;;
    --cluster-pool)         mode_cluster_pool ;;
    --pool-only)            mode_pool_only ;;
    --status)               mode_status ;;
    --parallel-download)    mode_parallel_download ;;
    --stop)                 mode_stop ;;
    -h|--help)
        sed -n '2,/^$/p' "$0" | head -25
        exit 0
        ;;
    "")                     main_menu ;;
    *)                      err "Argument inconnu : $1"; exit 1 ;;
esac
