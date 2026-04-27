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
    [string]$CudaIndex = "https://download.pytorch.org/whl/cu130",
    [switch]$SkipModels,
    [switch]$SkipWorkflows,
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
Write-Host "[1/7] Checking prerequisites..."
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
    Write-Host "[2/7] Cloning ComfyUI..."
    git clone https://github.com/comfyanonymous/ComfyUI.git $InstallPath
} else {
    Write-Host "[2/7] ComfyUI dir already exists at $InstallPath — pulling latest"
    git -C $InstallPath pull --rebase
}

# ── 3. Installer torch CUDA + dependencies ──────────────────────────────────
Write-Host "[3/7] Installing PyTorch (CUDA) + ComfyUI requirements..."
& python -m pip install --upgrade pip
& python -m pip install torch torchvision torchaudio --extra-index-url $CudaIndex
& python -m pip install -r "$InstallPath\requirements.txt"

# Sage attention + Triton for Windows (Blackwell)
Write-Host "  Installing sageattention + triton-windows..."
& python -m pip install sageattention
& python -m pip install triton-windows

# Frontend latest
& python -m pip install -U comfyui_frontend_package

# Verify
Write-Host "  Verification :"
& python -c "import torch; print(f'  torch={torch.__version__}, cuda={torch.cuda.is_available()}, device={torch.cuda.get_device_name(0) if torch.cuda.is_available() else None}')"

# ── 4. Custom nodes ─────────────────────────────────────────────────────────
Write-Host "[4/7] Installing custom nodes..."
$nodesDir = Join-Path $InstallPath "custom_nodes"
New-Item -ItemType Directory -Force -Path $nodesDir | Out-Null

# ComfyUI-Manager
if (-not (Test-Path "$nodesDir\ComfyUI-Manager")) {
    git clone https://github.com/Comfy-Org/ComfyUI-Manager.git "$nodesDir\ComfyUI-Manager"
}

# Notre extension workflow-manager — copie depuis le repo source
$wmSrc = Join-Path $root "comfyui-workflow-manager"
if (Test-Path $wmSrc) {
    $wmDest = Join-Path $nodesDir "comfyui-workflow-manager"
    if (-not (Test-Path $wmDest)) {
        Copy-Item -Recurse $wmSrc $wmDest
        Write-Host "  ✓ comfyui-workflow-manager linked"
    }
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
Write-Host "[5/7] Adding firewall rule for port 8188..."
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
    Write-Host "[6/7] Installing all local workflows into user/default/workflows..."
    $catScript = Join-Path $PSScriptRoot "comfyui_catalog.py"
    if (Test-Path $catScript) {
        & python $catScript --comfyui-path $InstallPath install-workflows
    } else {
        Write-Warning "comfyui_catalog.py introuvable — saut de cette étape"
    }
} else {
    Write-Host "[6/7] (skipped) Workflows installation"
}

# ── 7. Modèles ──────────────────────────────────────────────────────────────
if (-not $SkipModels) {
    Write-Host "[7/7] Building catalog and downloading latest models (budget=$Budget GB)..."
    Write-Host ""
    Write-Host "  ⚠  Lance ComfyUI dans un autre terminal avant de continuer :"
    Write-Host ""
    Write-Host "      cd '$InstallPath'"
    Write-Host "      python main.py --listen 0.0.0.0 --port 8188 --disable-xformers --use-pytorch-cross-attention"
    Write-Host ""
    Write-Host "  Puis dans ce terminal :"
    Write-Host "      python '$PSScriptRoot\comfyui_catalog.py' build --budget $Budget --max-age-years $MaxAgeYears"
    Write-Host "      python '$PSScriptRoot\comfyui_catalog.py' download"
} else {
    Write-Host "[7/7] (skipped) Models download"
}

Write-Host ""
Write-Host "✅ ComfyUI bootstrap done at $InstallPath"
