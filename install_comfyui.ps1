# Install ComfyUI + custom nodes + latest CUDA torch on a fresh Windows machine.
#
# Prerequisites :
#   - Python 3.13+ (https://www.python.org/downloads/)
#   - Git (https://git-scm.com/download/win)
#   - NVIDIA driver récent (RTX 5090 / sm_120 → driver >= 581)
#
# Usage :
#   pwsh ./scripts/install_comfyui.ps1 -InstallPath "C:\AI\ComfyUI"
#

param(
    [string]$InstallPath = "C:\Users\$env:USERNAME\creation-ops\ComfyUI",
    # PyTorch nightly with cu132 — required for RTX 5090 / Blackwell (sm_120).
    # Note: this MUST be --index-url (not --extra-index-url) so pip doesn't
    # silently fall back to PyPI's CPU-only wheels.
    [string]$CudaIndex = "https://download.pytorch.org/whl/nightly/cu132",
    [switch]$SkipModels,
    [switch]$SkipWorkflows,
    [switch]$NoVenv,                # disable venv creation if you want to use the system Python
    [int]$Budget = 1024,
    [int]$MaxAgeYears = 2
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $InstallPath

Write-Host "=== ComfyUI Bootstrap ==="
Write-Host "  Install path : $InstallPath"
Write-Host "  CUDA index   : $CudaIndex"
Write-Host "  Budget       : $Budget GB"
Write-Host ""

# ── 1. Pré-requis ──────────────────────────────────────────────────────────
Write-Host "[1/8] Checking prerequisites..."
foreach ($cmd in @("python", "git", "nvidia-smi")) {
    if (-not (Get-Command $cmd -ErrorAction SilentlyContinue)) {
        Write-Error "Manquant : $cmd. Installe-le et relance."
    }
}
$pyVer = & python --version
Write-Host "  ✓ $pyVer"
$gpuName = (& nvidia-smi --query-gpu=name --format=csv,noheader | Select-Object -First 1)
Write-Host "  ✓ GPU : $gpuName"

# ── 2. Clone ComfyUI ───────────────────────────────────────────────────────
if (-not (Test-Path $InstallPath)) {
    Write-Host "[2/8] Cloning ComfyUI..."
    git clone https://github.com/comfyanonymous/ComfyUI.git $InstallPath
} else {
    Write-Host "[2/8] ComfyUI dir already exists at $InstallPath — pulling latest"
    git -C $InstallPath pull --rebase
}

# ── 3. Create per-install venv ─────────────────────────────────────────────
$venvDir   = Join-Path $InstallPath "venv"
$venvPy    = Join-Path $venvDir "Scripts\python.exe"
$venvPip   = Join-Path $venvDir "Scripts\pip.exe"

if ($NoVenv) {
    Write-Host "[3/8] -NoVenv set → using system Python"
    $py = "python"
} else {
    if (-not (Test-Path $venvPy)) {
        Write-Host "[3/8] Creating venv at $venvDir ..."
        & python -m venv $venvDir
        if (-not (Test-Path $venvPy)) { Write-Error "venv creation failed" }
    } else {
        Write-Host "[3/8] venv already exists at $venvDir"
    }
    $py = $venvPy
}

# ── 4. PyTorch (nightly cu132 for Blackwell) + ComfyUI requirements ────────
Write-Host "[4/8] Installing PyTorch nightly + ComfyUI requirements..."
& $py -m pip install --upgrade pip
# CRITICAL: --index-url (not --extra-index-url) so pip won't silently pick
# PyPI's CPU-only wheels when a matching nightly wheel can't be found.
# --pre lets pip pick pre-release/nightly builds.
& $py -m pip install --pre torch torchvision torchaudio --index-url $CudaIndex
& $py -m pip install -r "$InstallPath\requirements.txt"

# Sage attention + Triton for Windows (Blackwell)
Write-Host "  Installing sageattention + triton-windows..."
& $py -m pip install sageattention
& $py -m pip install triton-windows

# Frontend latest
& $py -m pip install -U comfyui_frontend_package

# Verify CUDA actually works
Write-Host "  Verification :"
& $py -c "import torch; print(f'  torch={torch.__version__}, cuda={torch.cuda.is_available()}, device={torch.cuda.get_device_name(0) if torch.cuda.is_available() else None}')"
$cudaCheck = & $py -c "import torch; print('OK' if torch.cuda.is_available() else 'NOCUDA')"
if ($cudaCheck.Trim() -ne "OK") {
    Write-Warning "torch ne voit pas de GPU CUDA. Le script continue mais ComfyUI tournera en CPU."
    Write-Warning "Vérifie que ton driver NVIDIA est compatible avec cu132 (driver >= 581 pour Blackwell)."
}

# ── 4. Custom nodes ─────────────────────────────────────────────────────────
Write-Host "[5/8] Installing custom nodes..."
$nodesDir = Join-Path $InstallPath "custom_nodes"
New-Item -ItemType Directory -Force -Path $nodesDir | Out-Null

# ComfyUI-Manager
if (-not (Test-Path "$nodesDir\ComfyUI-Manager")) {
    git clone https://github.com/Comfy-Org/ComfyUI-Manager.git "$nodesDir\ComfyUI-Manager"
}

# Notre extension workflow-manager
# 1. Si une source locale existe (dev workflow) → copie
# 2. Sinon → clone depuis GitHub
$wmDest = Join-Path $nodesDir "comfyui-workflow-manager"
if (-not (Test-Path $wmDest)) {
    $wmLocalSources = @(
        (Join-Path $root "comfyui-workflow-manager"),
        (Join-Path $root "ComfyUI\custom_nodes\comfyui-workflow-manager"),
        "C:\Users\sshuser\creation-ops\ComfyUI\custom_nodes\comfyui-workflow-manager",
        "C:\Users\sshuser\creation-ops\comfyui-workflow-manager"
    )
    $foundLocal = $false
    foreach ($src in $wmLocalSources) {
        if (Test-Path $src) {
            Copy-Item -Recurse $src $wmDest
            Write-Host "  ✓ comfyui-workflow-manager copied from $src"
            $foundLocal = $true
            break
        }
    }
    if (-not $foundLocal) {
        Write-Host "  ⤓ Cloning comfyui-workflow-manager from GitHub..."
        git clone https://github.com/blackmagic42/comfyui-workflow-manager.git $wmDest
        Write-Host "  ✓ comfyui-workflow-manager cloned"
    }
} else {
    Write-Host "  ✓ comfyui-workflow-manager already present (skipping)"
}

# Magie Noir (junction si dispo)
$mnSrc = Join-Path $root "version5\version5"
if (Test-Path $mnSrc) {
    $mnDest = "$nodesDir\comfyui-magie-noir"
    $mnSrcInExt = Join-Path $root "ComfyUI\custom_nodes\comfyui-magie-noir"
    if (Test-Path $mnSrcInExt) {
        Copy-Item -Recurse $mnSrcInExt $mnDest -ErrorAction SilentlyContinue
    }
    # Junction to magie-noir project
    $junctionPath = Join-Path $mnDest "magie-noir"
    if (-not (Test-Path $junctionPath)) {
        New-Item -ItemType Junction -Path $junctionPath -Target $mnSrc -ErrorAction SilentlyContinue | Out-Null
        Write-Host "  ✓ magie-noir junction created"
    }
}

# ── 5. Pare-feu (port 8188) ─────────────────────────────────────────────────
Write-Host "[6/8] Adding firewall rule for port 8188..."
try {
    New-NetFirewallRule -DisplayName "ComfyUI 8188" -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8188 -Profile Any -ErrorAction Stop | Out-Null
    Write-Host "  ✓ rule added"
} catch {
    if ($_.Exception.Message -match "already exists") {
        Write-Host "  ⊖ rule already exists"
    } else {
        Write-Warning "Firewall rule failed (besoin d'admin?) : $($_.Exception.Message)"
    }
}

# ── 6. Workflows ────────────────────────────────────────────────────────────
if (-not $SkipWorkflows) {
    Write-Host "[7/8] Installing all local workflows into user/default/workflows..."
    $catScript = Join-Path $PSScriptRoot "comfyui_catalog.py"
    if (Test-Path $catScript) {
        & $py $catScript --comfyui-path $InstallPath install-workflows
    } else {
        Write-Warning "comfyui_catalog.py introuvable — saut de cette étape"
    }
} else {
    Write-Host "[7/8] (skipped) Workflows installation"
}

# ── 8. Modèles ──────────────────────────────────────────────────────────────
if (-not $SkipModels) {
    Write-Host "[8/8] Building catalog and downloading latest models (budget=$Budget GB)..."
    Write-Host ""
    Write-Host "  ⚠  Lance ComfyUI dans un autre terminal avant de continuer :"
    Write-Host ""
    Write-Host "      cd '$InstallPath'"
    Write-Host "      & '$venvPy' main.py --listen 0.0.0.0 --port 8188 --use-pytorch-cross-attention"
    Write-Host ""
    Write-Host "  Puis dans ce terminal :"
    Write-Host "      & '$venvPy' '$PSScriptRoot\comfyui_catalog.py' build --budget $Budget --max-age-years $MaxAgeYears"
    Write-Host "      & '$venvPy' '$PSScriptRoot\comfyui_catalog.py' download"
} else {
    Write-Host "[8/8] (skipped) Models download"
}

Write-Host ""
Write-Host "✅ ComfyUI bootstrap done at $InstallPath"
Write-Host ""
Write-Host "Pour démarrer ComfyUI (utilise le venv qu'on vient de créer) :"
Write-Host "      & '$venvPy' '$InstallPath\main.py' --listen 0.0.0.0 --port 8188"
Write-Host ""
Write-Host "Ou pour activer le venv interactivement :"
Write-Host "      & '$venvDir\Scripts\Activate.ps1'"
Write-Host "      cd '$InstallPath'"
Write-Host "      python main.py --port 8188"
