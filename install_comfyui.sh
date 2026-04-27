#!/usr/bin/env bash
# install_comfyui.sh — Bootstrap complet ComfyUI + custom nodes + modèles + workflows.
#
# Marche sur :
#   - Linux natif (bash)
#   - Windows via Git Bash / WSL
#   - macOS (avec adaptation CUDA → MPS)
#
# Usage :
#   bash install_comfyui.sh [options]
#
# Options :
#   --install-path PATH    Chemin d'installation (défaut: ~/comfyui)
#   --budget GB            Budget de stockage en GB (défaut: 1024)
#   --max-age-years N      Âge max des modèles (défaut: 2)
#   --skip-models          Ne pas télécharger les modèles
#   --skip-workflows       Ne pas copier les workflows
#   --no-magie-noir        Skip l'installation de l'extension magie-noir
#   --cuda-version N       Version CUDA (défaut: cu130)
#   --no-orchestrator      Ne pas lancer l'orchestrateur en fin d'install
#   --orchestrator-port P  Port de l'orchestrateur (défaut: 9000)
#   --shared-models PATH   Symlink ComfyUI/models → PATH (typiquement /mnt/shared)
#                          Permet à N machines du réseau de partager les modèles
#   --primary              Marque ce host comme primary (responsable des downloads)
#                          Les non-primary skip le téléchargement
#

set -euo pipefail

# ── Defaults ─────────────────────────────────────────────────────────────────
INSTALL_PATH="${HOME}/comfyui"
BUDGET=1024
MAX_AGE_YEARS=2
SKIP_MODELS=0
SKIP_WORKFLOWS=0
NO_MAGIE_NOIR=0
NO_ORCHESTRATOR=0
ORCHESTRATOR_PORT=9000
CUDA_VERSION="nightly/cu132"   # nightly cu132 — required for RTX 5090 / Blackwell sm_120
SHARED_MODELS=""
IS_PRIMARY=0

# Detect OS
case "$(uname -s 2>/dev/null || echo Windows)" in
    Linux*)   OS=linux ;;
    Darwin*)  OS=mac ;;
    MINGW*|MSYS*|CYGWIN*|Windows*) OS=windows ;;
    *)        OS=unknown ;;
esac

# ── Parse args ───────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --install-path)  INSTALL_PATH="$2"; shift 2 ;;
        --budget)        BUDGET="$2"; shift 2 ;;
        --max-age-years) MAX_AGE_YEARS="$2"; shift 2 ;;
        --skip-models)   SKIP_MODELS=1; shift ;;
        --skip-workflows) SKIP_WORKFLOWS=1; shift ;;
        --no-magie-noir)      NO_MAGIE_NOIR=1; shift ;;
        --cuda-version)       CUDA_VERSION="$2"; shift 2 ;;
        --no-orchestrator)    NO_ORCHESTRATOR=1; shift ;;
        --orchestrator-port)  ORCHESTRATOR_PORT="$2"; shift 2 ;;
        --shared-models)      SHARED_MODELS="$2"; shift 2 ;;
        --primary)            IS_PRIMARY=1; shift ;;
        -h|--help)
            sed -n '2,/^$/p' "$0" | head -25
            exit 0
            ;;
        *) echo "Unknown arg: $1" >&2; exit 1 ;;
    esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

step() { echo "" ; echo "=== [$1] $2 ===" ; }
ok()   { echo "  ✓ $*" ; }
warn() { echo "  ⚠ $*" >&2 ; }
fail() { echo "  ✗ $*" >&2 ; exit 1 ; }

step "0/10" "Configuration"
echo "  OS                 : $OS"
echo "  Hostname           : $(hostname)"
echo "  Install path       : $INSTALL_PATH"
echo "  CUDA               : $CUDA_VERSION"
echo "  Budget             : $BUDGET GB"
echo "  Max age            : $MAX_AGE_YEARS years"
echo "  Skip models        : $SKIP_MODELS"
echo "  Skip workflows     : $SKIP_WORKFLOWS"
echo "  Shared models      : ${SHARED_MODELS:-<local>}"
echo "  Role               : $([[ $IS_PRIMARY -eq 1 ]] && echo 'primary (downloads)' || echo 'worker (uses shared)')"

# ── 1. Pré-requis ────────────────────────────────────────────────────────────
step "1/10" "Vérification des pré-requis"

# Detect python : prefer python3, fallback to python
PYTHON=""
for cand in python3 python; do
    if command -v "$cand" >/dev/null 2>&1; then
        PYTHON="$cand"
        break
    fi
done

if [[ -z "$PYTHON" ]]; then
    warn "python introuvable. Tentative d'auto-install..."
    if command -v apt-get >/dev/null 2>&1; then
        sudo apt-get update -qq && sudo apt-get install -y python3 python3-pip python3-venv git curl rsync
        PYTHON="python3"
    elif command -v dnf >/dev/null 2>&1; then
        sudo dnf install -y python3 python3-pip git curl rsync
        PYTHON="python3"
    elif command -v pacman >/dev/null 2>&1; then
        sudo pacman -Sy --noconfirm python python-pip git curl rsync
        PYTHON="python"
    else
        fail "Pas de package manager reconnu. Installe python3 manuellement."
    fi
fi
ok "python : $(command -v $PYTHON)"

# Ensure pip is available
if ! "$PYTHON" -m pip --version >/dev/null 2>&1; then
    warn "pip absent — install..."
    if command -v apt-get >/dev/null 2>&1; then
        sudo apt-get install -y python3-pip python3-venv
    elif command -v dnf >/dev/null 2>&1; then
        sudo dnf install -y python3-pip
    else
        "$PYTHON" -m ensurepip --upgrade 2>/dev/null || \
            curl -sS https://bootstrap.pypa.io/get-pip.py | "$PYTHON"
    fi
fi
ok "pip : $($PYTHON -m pip --version | head -1)"

# Other tools
for cmd in git; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
        warn "$cmd absent — install..."
        if command -v apt-get >/dev/null 2>&1; then sudo apt-get install -y "$cmd"; fi
    fi
    ok "$cmd : $(command -v $cmd)"
done

# Export PYTHON pour que les sous-commandes l'utilisent
export PYTHON
PY_VER=$($PYTHON -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
ok "Python : $PY_VER"

# nvidia-smi optional
if command -v nvidia-smi >/dev/null 2>&1; then
    GPU=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1 || echo "?")
    ok "GPU : $GPU"
else
    warn "nvidia-smi absent — installation CPU-only ou MPS"
fi

# ── 2. Clone / update ComfyUI ────────────────────────────────────────────────
step "2/10" "Cloner / mettre à jour ComfyUI"
if [[ ! -d "$INSTALL_PATH" ]]; then
    git clone https://github.com/comfyanonymous/ComfyUI.git "$INSTALL_PATH"
    ok "Cloned to $INSTALL_PATH"
else
    git -C "$INSTALL_PATH" pull --rebase || warn "git pull failed (peut-être pas un git repo)"
    ok "Updated existing $INSTALL_PATH"
fi

# ── 2.5 Shared models mount (cluster mode) ──────────────────────────────────
if [[ -n "$SHARED_MODELS" ]]; then
    step "2.5/10" "Shared models : $SHARED_MODELS"
    if [[ ! -d "$SHARED_MODELS" ]]; then
        warn "$SHARED_MODELS n'existe pas — vérifie le mount NFS/SMB."
        warn "L'install continue mais models/ sera local."
    else
        MODELS_DIR="${INSTALL_PATH}/models"
        # Backup existing local models if any
        if [[ -e "$MODELS_DIR" && ! -L "$MODELS_DIR" ]]; then
            BACKUP="${MODELS_DIR}.local-backup-$(date +%s)"
            mv "$MODELS_DIR" "$BACKUP"
            ok "Backup local models → $BACKUP"
        fi
        rm -f "$MODELS_DIR"
        ln -s "$SHARED_MODELS" "$MODELS_DIR"
        ok "Symlink ComfyUI/models → $SHARED_MODELS"
    fi
fi

# ── 3. Create per-install venv ──────────────────────────────────────────────
step "3/10" "Création du venv (isolé par instance)"
VENV_DIR="${INSTALL_PATH}/venv"
if [[ "$OS" == "windows" ]]; then
    VENV_PY="${VENV_DIR}/Scripts/python.exe"
else
    VENV_PY="${VENV_DIR}/bin/python"
fi

if [[ ! -x "$VENV_PY" ]]; then
    $PYTHON -m venv "$VENV_DIR"
    [[ -x "$VENV_PY" ]] || fail "venv creation failed"
    ok "venv créé : $VENV_DIR"
else
    ok "venv existant : $VENV_DIR"
fi
# Re-target $PYTHON to use the venv for the rest of the install
PYTHON="$VENV_PY"

# ── 4. PyTorch nightly + dépendances ComfyUI ────────────────────────────────
step "4/10" "PyTorch nightly cu132 + dépendances ComfyUI"
$PYTHON -m pip install --upgrade pip --quiet

if [[ "$OS" == "mac" ]]; then
    # mac → MPS, no CUDA
    $PYTHON -m pip install torch torchvision torchaudio --quiet
else
    # CRITICAL: --index-url (not --extra-index-url) so pip won't silently
    # pick PyPI's CPU-only wheels when a matching nightly wheel can't be found.
    # --pre lets pip pick pre-release/nightly builds.
    $PYTHON -m pip install --pre torch torchvision torchaudio \
        --index-url "https://download.pytorch.org/whl/${CUDA_VERSION}" --quiet
fi
ok "torch installed (nightly cu132)"

$PYTHON -m pip install -r "${INSTALL_PATH}/requirements.txt" --quiet
ok "ComfyUI requirements installed"

# Sage attention + Triton (Windows specifically uses triton-windows)
if [[ "$OS" == "windows" ]]; then
    $PYTHON -m pip install sageattention triton-windows --quiet || warn "sageattention/triton install non-fatal"
elif [[ "$OS" == "linux" ]]; then
    $PYTHON -m pip install sageattention triton --quiet || warn "sageattention/triton install non-fatal"
fi
ok "attention backends installed"

$PYTHON -m pip install -U comfyui_frontend_package --quiet
ok "frontend updated"

# Vérification
$PYTHON -c "
import torch
print(f'  torch={torch.__version__}, cuda={torch.cuda.is_available()}, ' \
      f'device={torch.cuda.get_device_name(0) if torch.cuda.is_available() else \"none\"}')"

# ── 4. Custom nodes ──────────────────────────────────────────────────────────
step "5/10" "Custom nodes"
NODES_DIR="${INSTALL_PATH}/custom_nodes"
mkdir -p "$NODES_DIR"

# ComfyUI-Manager
if [[ ! -d "${NODES_DIR}/ComfyUI-Manager" ]]; then
    git clone https://github.com/Comfy-Org/ComfyUI-Manager.git "${NODES_DIR}/ComfyUI-Manager"
    ok "ComfyUI-Manager cloned"
else
    git -C "${NODES_DIR}/ComfyUI-Manager" pull --rebase 2>/dev/null || true
fi

# Notre extension comfyui-workflow-manager
# 1. Si une source locale existe (dev workflow) → copie
# 2. Sinon → clone depuis GitHub
WM_DEST="${NODES_DIR}/comfyui-workflow-manager"
if [[ ! -d "$WM_DEST" ]]; then
    WM_FOUND=0
    for WM_SRC in \
        "${ROOT_DIR}/comfyui-workflow-manager" \
        "${ROOT_DIR}/ComfyUI/custom_nodes/comfyui-workflow-manager" \
        "${HOME}/creation-ops/ComfyUI/custom_nodes/comfyui-workflow-manager" \
        "${HOME}/creation-ops/comfyui-workflow-manager" \
    ; do
        if [[ -d "$WM_SRC" ]]; then
            cp -r "$WM_SRC" "$WM_DEST"
            ok "comfyui-workflow-manager copied from $WM_SRC"
            WM_FOUND=1
            break
        fi
    done
    if [[ $WM_FOUND -eq 0 ]]; then
        echo "  ⤓ Cloning comfyui-workflow-manager from GitHub..."
        git clone https://github.com/blackmagic42/comfyui-workflow-manager.git "$WM_DEST"
        ok "comfyui-workflow-manager cloned"
    fi
else
    ok "comfyui-workflow-manager already present (skipping)"
fi

# Magie Noir (optionnel)
if [[ $NO_MAGIE_NOIR -eq 0 ]]; then
    MN_SRC="${ROOT_DIR}/version5/version5"
    MN_EXT_SRC="${ROOT_DIR}/ComfyUI/custom_nodes/comfyui-magie-noir"
    MN_DEST="${NODES_DIR}/comfyui-magie-noir"
    if [[ -d "$MN_EXT_SRC" && ! -d "$MN_DEST" ]]; then
        cp -r "$MN_EXT_SRC" "$MN_DEST"
        ok "comfyui-magie-noir copied"
    fi
    # Junction/symlink to magie-noir project
    if [[ -d "$MN_SRC" && ! -e "${MN_DEST}/magie-noir" ]]; then
        if [[ "$OS" == "windows" ]]; then
            cmd //c mklink //J "${MN_DEST}\\magie-noir" "$MN_SRC" 2>/dev/null || warn "junction failed"
        else
            ln -s "$MN_SRC" "${MN_DEST}/magie-noir"
        fi
        ok "magie-noir symlink/junction created"
    fi
fi

# ── 5. Pare-feu (Windows) ────────────────────────────────────────────────────
step "6/10" "Pare-feu / port 8188"
if [[ "$OS" == "windows" ]]; then
    # Try via PowerShell — non-fatal if not admin
    powershell.exe -Command "
        try {
            New-NetFirewallRule -DisplayName 'ComfyUI 8188' -Direction Inbound -Action Allow \
                -Protocol TCP -LocalPort 8188 -Profile Any -ErrorAction Stop | Out-Null
            Write-Output '  ✓ rule added'
        } catch {
            if (\$_.Exception.Message -match 'already exists') { Write-Output '  ⊖ rule already exists' }
            else { Write-Output '  ⚠ firewall rule failed (need admin)' }
        }
    " 2>/dev/null || warn "Could not configure firewall (need admin)"
elif [[ "$OS" == "linux" ]]; then
    if command -v ufw >/dev/null 2>&1; then
        sudo ufw allow 8188/tcp 2>/dev/null || warn "ufw not configured"
    fi
    ok "Linux : ufw 8188 (no-op si pas configuré)"
fi

# ── 6. Workflows ─────────────────────────────────────────────────────────────
step "7/10" "Workflows locaux → user/default/workflows/"
if [[ $SKIP_WORKFLOWS -eq 0 ]]; then
    $PYTHON "${SCRIPT_DIR}/comfyui_catalog.py" \
        --comfyui-path "$INSTALL_PATH" install-workflows
else
    echo "  (skipped)"
fi

# ── 7. Modèles ───────────────────────────────────────────────────────────────
step "8/10" "Modèles latest (≤ ${MAX_AGE_YEARS}y, budget ${BUDGET}GB)"
if [[ $SKIP_MODELS -eq 0 ]]; then
    echo "  → Délégué au dashboard (onglet ⚙ Commands → ✨ Apply changes)"
    echo "    L'orchestrateur lancera ComfyUI automatiquement quand un job arrive."
else
    echo "  (skipped)"
fi

# ── 8. Pré-classification + export API workflows ────────────────────────────
step "9/10" "Classification + export API workflows"
if [[ -f "${SCRIPT_DIR}/classify_workflows.py" ]]; then
    $PYTHON "${SCRIPT_DIR}/classify_workflows.py" 2>&1 | tail -3 || warn "classify failed"
    ok "Workflows classifiés"
fi
if [[ -f "${SCRIPT_DIR}/export_workflows_api.py" ]]; then
    $PYTHON "${SCRIPT_DIR}/export_workflows_api.py" 2>&1 | tail -3 || warn "export API failed"
fi

# ── 9. Auto-launch orchestrator ──────────────────────────────────────────────
step "10/10" "Lancement de l'orchestrateur"
TOKEN=""
if [[ $NO_ORCHESTRATOR -eq 0 ]]; then
    # Stop any existing orchestrator on this port
    if [[ "$OS" == "windows" ]]; then
        powershell.exe -Command "Get-NetTCPConnection -LocalPort ${ORCHESTRATOR_PORT} -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id \$_.OwningProcess -Force -ErrorAction SilentlyContinue }" 2>/dev/null || true
    elif command -v fuser >/dev/null 2>&1; then
        fuser -k "${ORCHESTRATOR_PORT}/tcp" 2>/dev/null || true
    fi
    sleep 1

    LOG_FILE="${SCRIPT_DIR}/../.catalog_state/orchestrator.log"
    mkdir -p "$(dirname "$LOG_FILE")"

    if [[ "$OS" == "windows" ]]; then
        cmd.exe //c "start /b $PYTHON \"${SCRIPT_DIR}/orchestrator.py\" serve --port ${ORCHESTRATOR_PORT} > \"${LOG_FILE}\" 2>&1" 2>/dev/null
    else
        nohup $PYTHON "${SCRIPT_DIR}/orchestrator.py" serve --port "$ORCHESTRATOR_PORT" \
              > "$LOG_FILE" 2>&1 &
        disown
    fi

    sleep 4
    if command -v curl >/dev/null 2>&1 && curl --max-time 3 -s -o /dev/null "http://127.0.0.1:${ORCHESTRATOR_PORT}/dashboard" 2>/dev/null; then
        ok "Orchestrateur live sur http://127.0.0.1:${ORCHESTRATOR_PORT}/dashboard"
    else
        warn "Démarrage en cours, vérifie le log : $LOG_FILE"
    fi

    TOKEN_FILE="${SCRIPT_DIR}/../.catalog_state/auth_token"
    [[ -f "$TOKEN_FILE" ]] && TOKEN=$(cat "$TOKEN_FILE")
else
    echo "  (skipped — utilise --no-orchestrator pour ré-activer)"
fi

# ── Final summary ────────────────────────────────────────────────────────────
echo ""
echo "═══════════════════════════════════════════════════════════════════════"
echo "✅ Installation terminée"
echo "═══════════════════════════════════════════════════════════════════════"
echo ""
echo "  ComfyUI       : $INSTALL_PATH"
echo "  Custom nodes  : ${NODES_DIR}/"
echo "  Scripts       : ${SCRIPT_DIR}/"
echo ""
if [[ $NO_ORCHESTRATOR -eq 0 ]]; then
    echo "🎛  Dashboard  : http://127.0.0.1:${ORCHESTRATOR_PORT}/dashboard"
    echo "    (depuis une autre machine : remplace 127.0.0.1 par l'IP locale)"
    if [[ -n "$TOKEN" ]]; then
        echo "🔑 Token      : $TOKEN"
        echo "    (à coller au prompt du dashboard)"
    fi
    echo ""
    echo "  Logs : ${LOG_FILE:-non démarré}"
    echo "  Stop : kill \$(lsof -ti:${ORCHESTRATOR_PORT})  (Linux/Mac)"
    echo ""
fi
echo "Prochaines étapes (depuis le dashboard onglet ⚙ Commands) :"
echo "  1. 📦 Choisis un Bundle (Minimal/Image/Standard/Full) ou règle le slider"
echo "  2. ✨ Apply changes (build + download + cleanup)"
echo "  3. 🚀 Launch ComfyUI instance (si pas auto)"
echo ""
