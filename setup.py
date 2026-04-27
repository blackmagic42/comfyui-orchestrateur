#!/usr/bin/env python3
"""
setup.py — Cross-platform interactive launcher AND lifecycle manager for the
ComfyUI install/cluster stack.

Runs on Windows (PowerShell or cmd) and Linux/macOS (any shell). Wraps:
  - install_comfyui.sh   (Linux/macOS)
  - install_comfyui.ps1  (Windows)
  - deploy_cluster.sh    (cluster ops, requires bash + ssh)

First run: install. Re-run: see what's installed, start/stop/open instances.

Modes offered by the menu:
  m. Manage existing instances (start, stop, open dashboard, delete)
  1. Single-instance install on this machine
  2. Multi-instance install on this machine (N disks, N ports)
  3. Cluster: shared NFS (1 primary downloads, workers read from NFS)
  4. Cluster: pool mode (each host stores 1/N, sees N/N via mergerfs)
  5. Pool config only (cluster already installed)
  6. Status of a running cluster
  7. Parallel download (cluster)
  8. Stop cluster

State (registered instances) lives in:
  ~/.comfyui-stack/registry.json

No external dependencies — stdlib only.
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import signal
import socket
import subprocess
import sys
import time
import webbrowser
from datetime import datetime
from pathlib import Path

# Force UTF-8 stdout/stderr so the Unicode banner & glyphs render under
# Windows pipes / non-UTF-8 consoles (default is cp1252 there).
for _stream_name in ("stdout", "stderr"):
    _s = getattr(sys, _stream_name, None)
    if _s is not None and hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

SCRIPT_DIR = Path(__file__).resolve().parent
IS_WIN = os.name == "nt"
IS_LINUX = sys.platform.startswith("linux")
IS_MAC = sys.platform == "darwin"

# ── Colors (ANSI; Windows 10+ Terminal handles them natively) ───────────────
USE_COLOR = sys.stdout.isatty()
def _c(code: str) -> str: return code if USE_COLOR else ""
BOLD, DIM, RESET = _c("\033[1m"), _c("\033[2m"), _c("\033[0m")
GREEN, YELLOW, BLUE, CYAN, RED = (_c(f"\033[3{n}m") for n in (2, 3, 4, 6, 1))

# Enable VT processing on legacy Windows consoles
if IS_WIN and USE_COLOR:
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if k32.GetConsoleMode(h, ctypes.byref(mode)):
            k32.SetConsoleMode(h, mode.value | 0x4)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
    except Exception:
        pass


# ── UI helpers ──────────────────────────────────────────────────────────────
def banner() -> None:
    print(f"""
{CYAN}{BOLD}  ╔═══════════════════════════════════════════════════════════════╗
  ║         ComfyUI — Installation & Cluster Manager              ║
  ║                  Cross-platform launcher                      ║
  ╚═══════════════════════════════════════════════════════════════╝{RESET}
  {DIM}OS detected: {platform.system()} {platform.release()} · Python {platform.python_version()}{RESET}
""")

def step(msg: str)  -> None: print(f"{BLUE}{BOLD}▸{RESET} {BOLD}{msg}{RESET}")
def ok(msg: str)    -> None: print(f"  {GREEN}✓{RESET} {msg}")
def warn(msg: str)  -> None: print(f"  {YELLOW}⚠{RESET} {msg}")
def err(msg: str)   -> None: print(f"  {RED}✗{RESET} {msg}", file=sys.stderr)
def hint(msg: str)  -> None: print(f"  {DIM}{msg}{RESET}")


def ask(prompt: str, default: str = "") -> str:
    suffix = f" {DIM}[{default}]{RESET}" if default else ""
    answer = input(f"{BOLD}? {RESET}{prompt}{suffix} ").strip()
    return answer or default


def ask_int(prompt: str, default: int, *, min_val: int = 1) -> int:
    while True:
        raw = ask(prompt, str(default))
        try:
            val = int(raw)
            if val < min_val:
                warn(f"Doit être ≥ {min_val}.")
                continue
            return val
        except ValueError:
            warn("Entre un nombre entier.")


def confirm(prompt: str, default: bool = True) -> bool:
    suffix = f" {DIM}[{'Y/n' if default else 'y/N'}]{RESET}"
    while True:
        raw = input(f"{BOLD}? {RESET}{prompt}{suffix} ").strip().lower()
        if not raw: return default
        if raw in ("y", "yes", "o", "oui"): return True
        if raw in ("n", "no", "non"): return False
        warn("Réponds y ou n.")


def read_hosts() -> list[str]:
    print(f"{BOLD}Entre tes hosts (format: {DIM}user@host{RESET}{BOLD}), un par ligne. Ligne vide = fin.{RESET}")
    hint("Exemple : dgx1@192.168.1.10")
    hosts: list[str] = []
    while True:
        line = input("  → ").strip()
        if not line:
            if not hosts:
                warn("Au moins un host requis.")
                continue
            return hosts
        hosts.append(line)


# ── Registry of installed instances ─────────────────────────────────────────
REGISTRY_PATH = Path.home() / ".comfyui-stack" / "registry.json"


class Registry:
    """Persistent record of installed ComfyUI instances and clusters.

    Schema:
      {"instances": [
        {"id": int, "name": str, "kind": "local"|"cluster",
         "install_path": str, "port": int, "orchestrator_port": int,
         "pid": int|None, "created_at": iso8601,
         # cluster-only:
         "hosts": [str], "primary": str, "shared_models": str|None}
      ]}
    """
    def __init__(self, path: Path = REGISTRY_PATH) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.data = self._load()

    def _load(self) -> dict:
        if not self.path.exists():
            return {"instances": []}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {"instances": []}

    def save(self) -> None:
        self.path.write_text(json.dumps(self.data, indent=2), encoding="utf-8")

    @property
    def instances(self) -> list[dict]:
        return self.data.setdefault("instances", [])

    def next_id(self) -> int:
        return max((i.get("id", 0) for i in self.instances), default=0) + 1

    def add(self, **fields) -> dict:
        # Avoid duplicates on the same install_path (re-install case): update.
        path = fields.get("install_path")
        if path:
            for inst in self.instances:
                if inst.get("install_path") == path and inst.get("kind") == fields.get("kind"):
                    inst.update(fields)
                    self.save()
                    return inst
        fields.setdefault("id", self.next_id())
        fields.setdefault("created_at", datetime.now().isoformat(timespec="seconds"))
        self.instances.append(fields)
        self.save()
        return fields

    def remove(self, idx_in_list: int) -> None:
        del self.instances[idx_in_list]
        self.save()

    def find_by_id(self, iid: int) -> int | None:
        for i, inst in enumerate(self.instances):
            if inst.get("id") == iid:
                return i
        return None


# ── Runtime helpers (port probe / start / stop / open) ───────────────────────
def is_port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.5) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        return s.connect_ex((host, port)) == 0
    except Exception:
        return False
    finally:
        s.close()


def pid_alive(pid: int) -> bool:
    if not pid: return False
    if IS_WIN:
        try:
            r = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=5,
            )
            return str(pid) in r.stdout
        except Exception:
            return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            return pid_alive_check_proc_root(pid)
        except Exception:
            return False


def pid_alive_check_proc_root(pid: int) -> bool:
    return Path(f"/proc/{pid}").exists() if IS_LINUX else False


def venv_python(install_path: Path) -> str:
    """Pick the venv Python if present, else the system one."""
    candidates = [
        install_path / "venv" / "Scripts" / "python.exe",
        install_path / "venv" / "bin" / "python",
        install_path / ".venv" / "Scripts" / "python.exe",
        install_path / ".venv" / "bin" / "python",
    ]
    for c in candidates:
        if c.exists(): return str(c)
    return "python"


def start_instance(inst: dict, registry: Registry) -> bool:
    """Spawn ComfyUI for an instance in the background. Detached process."""
    install_path = Path(inst["install_path"])
    if not (install_path / "main.py").exists():
        err(f"main.py introuvable dans {install_path}")
        return False
    port = int(inst.get("port", 8188))
    if is_port_open(port):
        warn(f"Port {port} est déjà utilisé — l'instance tourne peut-être déjà.")
        return False

    py = venv_python(install_path)
    cmd = [py, "main.py", "--listen", "0.0.0.0", "--port", str(port)]
    log_path = install_path / "comfyui.log"
    log_f = open(log_path, "ab")
    log_f.write(f"\n=== boot {datetime.now().isoformat()} ===\n".encode())

    try:
        if IS_WIN:
            DETACHED_PROCESS = 0x00000008
            proc = subprocess.Popen(
                cmd, cwd=str(install_path),
                stdin=subprocess.DEVNULL, stdout=log_f, stderr=log_f,
                creationflags=DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
                close_fds=True,
            )
        else:
            proc = subprocess.Popen(
                cmd, cwd=str(install_path),
                stdin=subprocess.DEVNULL, stdout=log_f, stderr=log_f,
                start_new_session=True, close_fds=True,
            )
    except Exception as e:
        err(f"Échec spawn : {e}")
        log_f.close()
        return False

    inst["pid"] = proc.pid
    inst["started_at"] = datetime.now().isoformat(timespec="seconds")
    registry.save()
    ok(f"Lancé · PID {proc.pid} · log : {log_path}")
    # Wait briefly for the port to come up so the user gets quick feedback
    for _ in range(20):
        time.sleep(0.5)
        if is_port_open(port):
            ok(f"Port {port} répond")
            return True
    warn(f"Port {port} ne répond pas encore (le boot peut prendre une minute) — voir log.")
    return True


def stop_instance(inst: dict, registry: Registry) -> bool:
    pid = inst.get("pid")
    port = int(inst.get("port", 8188))
    killed = False
    if pid:
        try:
            if IS_WIN:
                r = subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    capture_output=True, text=True, timeout=10,
                )
                killed = r.returncode == 0
            else:
                try: os.kill(pid, signal.SIGTERM)
                except ProcessLookupError: pass
                for _ in range(10):
                    if not pid_alive(pid): killed = True; break
                    time.sleep(0.3)
                if not killed:
                    try: os.kill(pid, signal.SIGKILL); killed = True
                    except ProcessLookupError: killed = True
        except Exception as e:
            warn(f"Échec kill PID {pid} : {e}")

    # Fallback: kill whoever holds the port (rare — pid might have changed)
    if not killed and is_port_open(port):
        if IS_WIN:
            try:
                r = subprocess.run(
                    ["powershell", "-NoProfile", "-Command",
                     f"Get-NetTCPConnection -LocalPort {port} -State Listen | "
                     f"Select-Object -ExpandProperty OwningProcess | "
                     f"ForEach-Object {{ Stop-Process -Id $_ -Force }}"],
                    capture_output=True, timeout=10,
                )
                killed = True
            except Exception: pass
        else:
            try:
                subprocess.run(["bash", "-c", f"kill $(lsof -ti:{port}) 2>/dev/null || true"],
                               timeout=10, check=False)
                killed = True
            except Exception: pass

    inst["pid"] = None
    inst["started_at"] = None
    registry.save()
    return killed


def instance_status(inst: dict) -> str:
    """Return one of: 'running', 'stopped', 'orphan', 'cluster'."""
    if inst.get("kind") == "cluster":
        return "cluster"
    port = int(inst.get("port", 8188))
    pid = inst.get("pid")
    if is_port_open(port):
        if pid and pid_alive(pid): return "running"
        return "orphan"  # port open but our recorded PID is dead
    return "stopped"


def open_dashboard(inst: dict) -> None:
    port = int(inst.get("port", 8188))
    url = f"http://127.0.0.1:{port}/"
    if not is_port_open(port):
        warn(f"Port {port} fermé — démarre l'instance d'abord.")
        return
    try:
        webbrowser.open(url)
        ok(f"Ouvert : {url}")
    except Exception as e:
        err(f"Impossible d'ouvrir : {e}")
        hint(f"Va manuellement sur {url}")


def tail_log(inst: dict, n: int = 50) -> None:
    install_path = Path(inst["install_path"])
    log_path = install_path / "comfyui.log"
    if not log_path.exists():
        warn(f"Pas de log à {log_path}")
        return
    try:
        with log_path.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 32 * 1024))
            chunk = f.read().decode("utf-8", errors="replace")
        lines = chunk.splitlines()[-n:]
        print()
        print(f"{DIM}── tail -{n} {log_path} ──{RESET}")
        for line in lines: print(line)
        print(f"{DIM}── end ──{RESET}")
    except Exception as e:
        err(f"Lecture log : {e}")


# ── Pré-vérifs ──────────────────────────────────────────────────────────────
def have(cmd: str) -> bool: return shutil.which(cmd) is not None


def pick_pwsh() -> str | None:
    """Return 'pwsh' (preferred) or 'powershell' if available, else None."""
    return "pwsh" if have("pwsh") else ("powershell" if have("powershell") else None)


def check_prereqs_local() -> bool:
    step("Vérification des pré-requis locaux")
    missing = [c for c in ("python", "git") if not have(c)]
    if missing:
        err(f"Outils manquants : {', '.join(missing)}")
        return False
    ok("python, git : OK")
    if IS_WIN:
        if pick_pwsh():
            ok(f"PowerShell : {pick_pwsh()}")
        else:
            err("PowerShell introuvable (pwsh / powershell). Install-le ou utilise Git Bash + setup.sh.")
            return False
    else:
        if not have("bash"):
            err("bash introuvable.")
            return False
        ok("bash : OK")
    if have("nvidia-smi"):
        ok("nvidia-smi détecté")
    else:
        warn("nvidia-smi absent — install passera en CPU/MPS")
    return True


def check_prereqs_cluster() -> bool:
    step("Vérification des pré-requis (poste de contrôle pour cluster)")
    needed = ("ssh", "rsync", "bash")
    missing = [c for c in needed if not have(c)]
    if missing:
        err(f"Outils manquants : {', '.join(missing)}")
        if IS_WIN:
            hint("Sur Windows, installe Git Bash (https://git-scm.com) — il fournit ssh, rsync et bash.")
        return False
    ok(f"{', '.join(needed)} : OK")
    return True


def ssh_check_hosts(hosts: list[str]) -> bool:
    step("Test de connectivité SSH")
    failed = 0
    for h in hosts:
        try:
            r = subprocess.run(
                ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", h, "true"],
                capture_output=True, timeout=10,
            )
            if r.returncode == 0:
                ok(h)
            else:
                err(f"{h} : code {r.returncode}")
                failed += 1
        except Exception as e:
            err(f"{h} : {e}")
            failed += 1
    return failed == 0


# ── Backend invokers ────────────────────────────────────────────────────────
def run_install_local(install_path: str, budget: int, max_age: int,
                      *, port: int = 8188, orch_port: int = 9000,
                      skip_models: bool = False, skip_workflows: bool = False,
                      no_orchestrator: bool = False) -> int:
    """Run the per-OS installer for a single ComfyUI instance."""
    if IS_WIN:
        ps = pick_pwsh()
        if not ps:
            err("PowerShell introuvable.")
            return 1
        # The PS installer doesn't yet take port/orchestrator flags; we only
        # forward the args it understands. Multi-instance port wiring is
        # handled by emitting a launch script per instance below.
        cmd = [
            ps, "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", str(SCRIPT_DIR / "install_comfyui.ps1"),
            "-InstallPath", install_path,
            "-Budget", str(budget),
            "-MaxAgeYears", str(max_age),
        ]
        if skip_models:    cmd.append("-SkipModels")
        if skip_workflows: cmd.append("-SkipWorkflows")
    else:
        cmd = [
            "bash", str(SCRIPT_DIR / "install_comfyui.sh"),
            "--install-path", install_path,
            "--budget", str(budget),
            "--max-age-years", str(max_age),
            "--orchestrator-port", str(orch_port),
        ]
        if skip_models:     cmd.append("--skip-models")
        if skip_workflows:  cmd.append("--skip-workflows")
        if no_orchestrator: cmd.append("--no-orchestrator")
    print()
    step(f"Lancement : {' '.join(cmd[:3])} …")
    return subprocess.call(cmd)


def emit_launch_script(install_path: str, port: int, orch_port: int) -> Path:
    """Generate a per-instance launch script that uses the local venv if present.

    install_comfyui.sh / .ps1 create a venv at <install_path>/venv with the
    correct CUDA-enabled torch — we MUST use that python, not the system one,
    or ComfyUI will hit "Torch not compiled with CUDA enabled".
    """
    install_path_p = Path(install_path)
    venv_py_win  = install_path_p / "venv" / "Scripts" / "python.exe"
    venv_py_unix = install_path_p / "venv" / "bin" / "python"
    if IS_WIN:
        path = install_path_p / "start_instance.ps1"
        path.write_text(
            f"# Auto-generated launcher\n"
            f"$ErrorActionPreference = 'Stop'\n"
            f"Set-Location {install_path_p}\n"
            f"$venvPy = '{venv_py_win}'\n"
            f"if (Test-Path $venvPy) {{\n"
            f"    & $venvPy main.py --listen 0.0.0.0 --port {port}\n"
            f"}} else {{\n"
            f"    Write-Host 'venv introuvable, fallback python système (CUDA peut ne pas marcher)'\n"
            f"    python main.py --listen 0.0.0.0 --port {port}\n"
            f"}}\n",
            encoding="utf-8",
        )
    else:
        path = install_path_p / "start_instance.sh"
        path.write_text(
            f"#!/usr/bin/env bash\n"
            f"set -e\n"
            f"cd {install_path}\n"
            f"VENV_PY={venv_py_unix}\n"
            f"if [[ -x \"$VENV_PY\" ]]; then\n"
            f"    exec \"$VENV_PY\" main.py --listen 0.0.0.0 --port {port}\n"
            f"else\n"
            f"    echo 'venv introuvable, fallback python système (CUDA peut ne pas marcher)'\n"
            f"    exec python main.py --listen 0.0.0.0 --port {port}\n"
            f"fi\n"
        )
        path.chmod(0o755)
    return path


def run_bash_script(name: str, args: list[str]) -> int:
    """Invoke a bash script from SCRIPT_DIR with args. Used for cluster ops."""
    if not have("bash"):
        err("bash introuvable — installe Git Bash sur Windows ou utilise Linux.")
        return 1
    cmd = ["bash", str(SCRIPT_DIR / name), *args]
    step(f"Lancement : bash {name} …")
    return subprocess.call(cmd)


# ── Modes ───────────────────────────────────────────────────────────────────
def mode_single() -> None:
    banner()
    step(f"Mode : {BOLD}Installation sur cette machine{RESET}")
    print()
    if not check_prereqs_local():
        return

    default_path = (
        f"C:\\Users\\{os.environ.get('USERNAME', 'user')}\\creation-ops\\ComfyUI"
        if IS_WIN else f"{Path.home()}/comfyui"
    )
    install_path  = ask("Chemin d'installation", default_path)
    budget        = ask_int("Budget de stockage (GB)", 1024)
    max_age       = ask_int("Âge max des modèles (années)", 2)
    skip_models   = not confirm("Télécharger les modèles maintenant ? (sinon depuis le dashboard)", False)
    skip_wf       = not confirm("Copier les 218 workflows de référence ?", True)
    orch_port     = 9000
    no_orch       = False
    if not IS_WIN:
        orch_port = ask_int("Port de l'orchestrateur", 9000)
        no_orch   = not confirm("Lancer l'orchestrateur en fin d'install ?", True)

    print()
    step("Récap")
    print(f"  Install path     : {BOLD}{install_path}{RESET}")
    print(f"  Budget           : {BOLD}{budget} GB{RESET}")
    print(f"  Max age          : {max_age} ans")
    if not IS_WIN:
        print(f"  Orchestrator     : port {orch_port}{' (skip)' if no_orch else ''}")
    print(f"  Skip models DL   : {YELLOW + 'oui' + RESET if skip_models else 'non'}")
    print(f"  Skip workflows   : {YELLOW + 'oui' + RESET if skip_wf else 'non'}")
    print()
    if not confirm("Lancer l'installation ?", True):
        warn("Annulé.")
        return

    rc = run_install_local(
        install_path, budget, max_age,
        skip_models=skip_models, skip_workflows=skip_wf,
        orch_port=orch_port, no_orchestrator=no_orch,
    )
    if rc == 0:
        ok("Installation terminée.")
        registry = Registry()
        registry.add(
            kind="local",
            name=Path(install_path).name or "comfyui",
            install_path=install_path,
            port=8188,
            orchestrator_port=orch_port,
            budget_gb=budget,
        )
        ok("Enregistrée dans le registre — re-lance setup.py pour la gérer.")
    else:
        err(f"Installation a échoué (code {rc}).")


def mode_multi_instance() -> None:
    banner()
    step(f"Mode : {BOLD}Plusieurs ComfyUI sur cette machine{RESET}")
    print()
    hint("Idéal si tu as plusieurs disques (ex: D:\\ rapide pour Flux, E:\\ pour SD3).")
    hint("Chaque instance a son propre dossier, son propre port, son propre cache.")
    print()
    if not check_prereqs_local():
        return

    n = ask_int("Combien d'instances installer ?", 2, min_val=1)
    print()

    instances: list[dict] = []
    for i in range(1, n + 1):
        print(f"{CYAN}── Instance {i}/{n} ──{RESET}")
        default_path = (
            f"D:\\ComfyUI-{i}" if IS_WIN else f"/opt/comfyui-{i}"
        ) if i == 1 else (
            (f"E:\\ComfyUI-{i}" if IS_WIN else f"/mnt/disk{i}/comfyui")
            if i == 2 else (f"X:\\ComfyUI-{i}" if IS_WIN else f"/mnt/disk{i}/comfyui")
        )
        # Better: just suggest a numeric default per instance
        default_path = (f"D:\\ComfyUI-{i}" if IS_WIN else f"{Path.home()}/comfyui-{i}")
        path = ask(f"  Path instance {i}", default_path)
        port = ask_int(f"  Port ComfyUI instance {i}", 8188 + (i - 1))
        budget = ask_int(f"  Budget GB instance {i}", 512)
        instances.append({"path": path, "port": port, "budget": budget})
        print()

    skip_models = not confirm("Télécharger les modèles pour chaque instance ? (sinon : depuis dashboard)", False)
    skip_wf     = not confirm("Copier les workflows de référence dans chaque instance ?", True)
    max_age     = ask_int("Âge max des modèles (années)", 2)

    print()
    step("Récap des instances")
    for i, inst in enumerate(instances, 1):
        print(f"  {i}. {BOLD}{inst['path']}{RESET} · port {inst['port']} · {inst['budget']} GB")
    print()
    if not confirm("Installer toutes ces instances ?", True):
        warn("Annulé.")
        return

    registry = Registry()
    failures = 0
    for i, inst in enumerate(instances, 1):
        print()
        step(f"=== Instance {i}/{n}: {inst['path']} ===")
        rc = run_install_local(
            inst["path"], inst["budget"], max_age,
            port=inst["port"],
            orch_port=9000 + (i - 1),
            skip_models=skip_models, skip_workflows=skip_wf,
            no_orchestrator=True,  # always skip auto-launch in multi-instance mode
        )
        if rc != 0:
            err(f"Instance {i} a échoué — on continue avec les suivantes.")
            failures += 1
            continue
        try:
            launcher = emit_launch_script(inst["path"], inst["port"], 9000 + (i - 1))
            ok(f"Launcher généré : {launcher}")
        except Exception as e:
            warn(f"Impossible de générer le launcher : {e}")
        registry.add(
            kind="local",
            name=Path(inst["path"]).name or f"comfyui-{i}",
            install_path=inst["path"],
            port=inst["port"],
            orchestrator_port=9000 + (i - 1),
            budget_gb=inst["budget"],
        )

    print()
    if failures == 0:
        ok(f"{n} instances installées.")
    else:
        warn(f"{failures}/{n} échecs — vérifie les logs.")
    print()
    step("Pour démarrer une instance manuellement :")
    for i, inst in enumerate(instances, 1):
        if IS_WIN:
            print(f"  {DIM}# Instance {i}{RESET}")
            print(f"  pwsh {inst['path']}\\start_instance.ps1")
        else:
            print(f"  {DIM}# Instance {i}{RESET}")
            print(f"  bash {inst['path']}/start_instance.sh")


def mode_cluster_shared() -> None:
    banner()
    step(f"Mode : {BOLD}Cluster avec modèles partagés (NFS){RESET}")
    print()
    hint("1 primary télécharge → workers utilisent un mount NFS.")
    print()
    if not check_prereqs_cluster():
        return

    hosts = read_hosts()
    print()
    step("Choix du primary")
    for i, h in enumerate(hosts, 1):
        print(f"  {i}. {h}")
    primary_idx = ask_int("Quel host est primary (numéro)", 1, min_val=1)
    if primary_idx > len(hosts):
        err("Index hors limites."); return
    primary = hosts[primary_idx - 1]

    shared = ask("Path NFS partagé (identique sur tous les hosts)", "/mnt/cluster_models")
    budget = ask_int("Budget total (GB)", 1500)
    max_age = ask_int("Âge max (années)", 2)
    orch_port = ask_int("Port orchestrateur", 9000)
    parallel = confirm("Lancer les installs en parallèle ?", True)

    print()
    step("Récap")
    print(f"  Hosts            : {' '.join(hosts)}")
    print(f"  Primary          : {BOLD}{primary}{RESET}")
    print(f"  Shared NFS       : {BOLD}{shared}{RESET}")
    print(f"  Budget           : {budget} GB · Max age : {max_age} ans")
    print(f"  Parallel install : {'oui' if parallel else 'non'}")
    print()
    if confirm("Tester SSH avant de lancer ?", True):
        if not ssh_check_hosts(hosts):
            if not confirm("Continuer malgré les erreurs ?", False): return

    if not confirm("Lancer le déploiement ?", True):
        warn("Annulé."); return

    args = [
        "--hosts", " ".join(hosts),
        "--primary", primary,
        "--shared-models", shared,
        "--orchestrator-port", str(orch_port),
        "--budget", str(budget),
        "--max-age-years", str(max_age),
    ]
    if parallel: args.append("--parallel")
    rc = run_bash_script("deploy_cluster.sh", args)
    if rc == 0:
        registry = Registry()
        registry.add(
            kind="cluster",
            cluster_mode="shared-nfs",
            name=f"cluster-{len(hosts)}-hosts",
            hosts=hosts,
            primary=primary,
            shared_models=shared,
            orchestrator_port=orch_port,
            budget_gb=budget,
            install_path=f"<cluster: {' '.join(hosts)}>",
        )
        ok("Cluster enregistré dans le registre.")


def mode_cluster_pool() -> None:
    banner()
    step(f"Mode : {BOLD}Cluster pool — chaque DG stocke 1/N, voit N/N{RESET}")
    print()
    hint("Catalogue total réparti entre hosts via NFS croisés + mergerfs.")
    print()
    if not check_prereqs_cluster():
        return

    hosts = read_hosts()
    if len(hosts) < 2:
        err("Le mode pool exige ≥ 2 hosts."); return

    step("Choix du primary")
    for i, h in enumerate(hosts, 1):
        print(f"  {i}. {h}")
    pi = ask_int("Primary (numéro)", 1)
    if pi > len(hosts): err("Index hors limites."); return
    primary = hosts[pi - 1]

    budget = ask_int("Budget total catalogue (GB)", 1500)
    max_age = ask_int("Âge max (années)", 2)
    install_first = confirm("Installer ComfyUI sur les hosts d'abord ?", True)

    print()
    step("Récap")
    print(f"  Hosts            : {' '.join(hosts)}")
    print(f"  Primary          : {BOLD}{primary}{RESET}")
    print(f"  Budget total     : {budget} GB (~{budget // len(hosts)} GB par host)")
    print(f"  Install d'abord  : {'oui' if install_first else 'non'}")
    print()
    if confirm("Tester SSH ?", True):
        if not ssh_check_hosts(hosts):
            if not confirm("Continuer ?", False): return
    if not confirm("Lancer la séquence ?", True):
        warn("Annulé."); return

    if install_first:
        step("Étape 1/3 — install parallèle")
        run_bash_script("deploy_cluster.sh", [
            "--hosts", " ".join(hosts),
            "--primary", primary,
            "--budget", str(budget),
            "--max-age-years", str(max_age),
            "--parallel",
        ])

    step("Étape 2/3 — setup-pool (NFS + mergerfs)")
    run_bash_script("deploy_cluster.sh", ["setup-pool", "--hosts", " ".join(hosts)])

    if confirm("Lancer le download parallèle maintenant ?", True):
        step("Étape 3/3 — parallel-download")
        run_bash_script("deploy_cluster.sh", [
            "parallel-download",
            "--hosts", " ".join(hosts),
            "--primary", primary,
        ])
    else:
        hint("Tu pourras le lancer plus tard via le menu (option 7).")

    registry = Registry()
    registry.add(
        kind="cluster",
        cluster_mode="pool-mergerfs",
        name=f"pool-{len(hosts)}-hosts",
        hosts=hosts,
        primary=primary,
        budget_gb=budget,
        install_path=f"<cluster pool: {' '.join(hosts)}>",
    )
    ok("Pool cluster enregistré dans le registre.")


def mode_pool_only() -> None:
    banner()
    step(f"Mode : {BOLD}Configurer le pool sur cluster déjà installé{RESET}")
    print()
    hosts = read_hosts()
    if not confirm("Lancer setup-pool ?", True): return
    run_bash_script("deploy_cluster.sh", ["setup-pool", "--hosts", " ".join(hosts)])


def mode_status() -> None:
    banner()
    step(f"Mode : {BOLD}Status du cluster{RESET}")
    print()
    hosts = read_hosts()
    run_bash_script("deploy_cluster.sh", ["pool-status", "--hosts", " ".join(hosts)])


def mode_parallel_download() -> None:
    banner()
    step(f"Mode : {BOLD}Parallel download{RESET}")
    print()
    hosts = read_hosts()
    print()
    for i, h in enumerate(hosts, 1): print(f"  {i}. {h}")
    pi = ask_int("Quel host a le manifest (primary)", 1)
    if pi > len(hosts): err("Index hors limites."); return
    primary = hosts[pi - 1]
    if not confirm("Lancer ?", True): return
    run_bash_script("deploy_cluster.sh", [
        "parallel-download",
        "--hosts", " ".join(hosts),
        "--primary", primary,
    ])


def mode_stop() -> None:
    banner()
    step(f"Mode : {BOLD}Stopper le cluster{RESET}")
    print()
    hosts = read_hosts()
    if not confirm(f"{RED}Confirmer l'arrêt sur {len(hosts)} host(s) ?{RESET}", False):
        warn("Annulé."); return
    for h in hosts:
        try:
            r = subprocess.run(
                ["ssh", h, "kill $(lsof -ti:9000) $(lsof -ti:8188) 2>/dev/null || true"],
                timeout=15,
            )
            if r.returncode == 0: ok(f"{h} : stoppé")
            else:                  warn(f"{h} : code {r.returncode}")
        except Exception as e:
            err(f"{h} : {e}")


# ── Manage mode (start/stop/open registered instances) ─────────────────────
def render_instance_row(idx: int, inst: dict) -> str:
    """One-line summary used in the main menu and manage list."""
    status = instance_status(inst)
    name = inst.get("name", "?")
    kind = inst.get("kind", "local")

    if status == "running":
        light = f"{GREEN}● running{RESET}"
    elif status == "orphan":
        light = f"{YELLOW}◐ orphan {RESET}"
    elif status == "cluster":
        light = f"{CYAN}☁ cluster{RESET}"
    else:
        light = f"{DIM}○ stopped{RESET}"

    if kind == "cluster":
        hosts = inst.get("hosts", [])
        mode = inst.get("cluster_mode", "?")
        loc = f"{len(hosts)} hosts · {mode}"
        port = ""
    else:
        loc = inst.get("install_path", "?")
        port = f":{inst.get('port', '?')}"

    return f"  {idx}. {light}  {BOLD}{name:<14}{RESET} {DIM}{loc}{RESET} {port}"


def render_instances(registry: Registry) -> bool:
    """Print the instances list. Return True if any."""
    if not registry.instances:
        return False
    print(f"{BOLD}📦 Instances enregistrées : {len(registry.instances)}{RESET}\n")
    for i, inst in enumerate(registry.instances, 1):
        print(render_instance_row(i, inst))
    print()
    return True


def manage_one(inst: dict, registry: Registry) -> None:
    while True:
        status = instance_status(inst)
        print()
        print(f"{CYAN}{BOLD}── Instance #{inst.get('id')} : {inst.get('name')} ──{RESET}")
        print(f"  Statut       : {status}")
        print(f"  Type         : {inst.get('kind', 'local')}")
        if inst.get("kind") == "cluster":
            print(f"  Mode cluster : {inst.get('cluster_mode', '?')}")
            print(f"  Hosts        : {' '.join(inst.get('hosts', []))}")
            print(f"  Primary      : {inst.get('primary', '?')}")
        else:
            print(f"  Path         : {inst.get('install_path')}")
            print(f"  Port         : {inst.get('port')}")
            print(f"  PID          : {inst.get('pid') or '—'}")
        print()

        if inst.get("kind") == "cluster":
            print(f"  {GREEN}s){RESET} Status (pool-status)")
            print(f"  {GREEN}d){RESET} Download parallèle")
            print(f"  {GREEN}x){RESET} Stopper le cluster")
            print(f"  {GREEN}r){RESET} Retirer du registre {DIM}(ne désinstalle pas){RESET}")
            print(f"  {GREEN}b){RESET} Retour")
            choice = ask("Action", "b").lower()
            if choice == "s":
                run_bash_script("deploy_cluster.sh", ["pool-status", "--hosts", " ".join(inst["hosts"])])
            elif choice == "d":
                run_bash_script("deploy_cluster.sh", [
                    "parallel-download",
                    "--hosts", " ".join(inst["hosts"]),
                    "--primary", inst["primary"],
                ])
            elif choice == "x":
                if confirm(f"{RED}Confirmer l'arrêt sur {len(inst['hosts'])} host(s) ?{RESET}", False):
                    for h in inst["hosts"]:
                        try:
                            subprocess.run(["ssh", h, "kill $(lsof -ti:9000) $(lsof -ti:8188) 2>/dev/null || true"], timeout=15)
                            ok(f"{h} stoppé")
                        except Exception as e:
                            err(f"{h}: {e}")
            elif choice == "r":
                if confirm("Retirer du registre ?", False):
                    pos = registry.find_by_id(inst["id"])
                    if pos is not None:
                        registry.remove(pos)
                        ok("Retiré.")
                        return
            else:
                return
        else:
            print(f"  {GREEN}s){RESET} Démarrer")
            print(f"  {GREEN}x){RESET} Arrêter")
            print(f"  {GREEN}o){RESET} Ouvrir le dashboard {DIM}(http://127.0.0.1:{inst.get('port')}/){RESET}")
            print(f"  {GREEN}l){RESET} Voir les logs (50 dernières lignes)")
            print(f"  {GREEN}r){RESET} Retirer du registre {DIM}(ne désinstalle pas){RESET}")
            print(f"  {GREEN}b){RESET} Retour")
            choice = ask("Action", "b").lower()
            if   choice == "s": start_instance(inst, registry)
            elif choice == "x":
                if confirm("Confirmer l'arrêt ?", True):
                    if stop_instance(inst, registry): ok("Arrêté.")
                    else:                              warn("Aucun process à tuer.")
            elif choice == "o": open_dashboard(inst)
            elif choice == "l": tail_log(inst)
            elif choice == "r":
                if confirm("Retirer du registre ?", False):
                    pos = registry.find_by_id(inst["id"])
                    if pos is not None:
                        registry.remove(pos)
                        ok("Retiré.")
                        return
            else:
                return


def mode_manage() -> None:
    banner()
    step(f"Mode : {BOLD}Gérer les instances enregistrées{RESET}")
    print()
    registry = Registry()
    if not registry.instances:
        warn("Aucune instance enregistrée. Lance d'abord une installation (option 1, 2, 3 ou 4).")
        return
    while True:
        if not render_instances(registry):
            return
        print(f"  {DIM}Tape un numéro pour gérer · 'b' pour revenir · 'r' pour rafraîchir{RESET}")
        choice = ask("Choix", "b").lower()
        if choice in ("b", "back", ""): return
        if choice == "r":
            registry = Registry()  # reload
            print()
            continue
        try:
            idx = int(choice)
        except ValueError:
            warn("Tape un numéro."); continue
        if idx < 1 or idx > len(registry.instances):
            warn("Numéro hors plage."); continue
        manage_one(registry.instances[idx - 1], registry)


# ── Main menu ───────────────────────────────────────────────────────────────
MODES = [
    ("1", "Installer ComfyUI sur cette machine (1 instance)",                       mode_single),
    ("2", "Installer plusieurs ComfyUI sur cette machine (multi-disques)",          mode_multi_instance),
    ("3", "Cluster — modèles partagés via NFS (1 primary + workers)",               mode_cluster_shared),
    ("4", "Cluster — pool mode (chaque DG stocke 1/N, voit N/N)",                   mode_cluster_pool),
    ("5", "Configurer le pool (cluster déjà installé)",                             mode_pool_only),
    ("6", "Status du cluster",                                                      mode_status),
    ("7", "Download parallèle (cluster pool déjà setup)",                           mode_parallel_download),
    ("8", "Stopper le cluster",                                                     mode_stop),
]

def main_menu() -> None:
    while True:
        banner()
        registry = Registry()
        has_instances = render_instances(registry)
        print(f"{BOLD}Que veux-tu faire ?{RESET}\n")
        if has_instances:
            print(f"  {GREEN}m){RESET} {BOLD}Gérer{RESET} les instances ci-dessus {DIM}(start, stop, ouvrir, logs){RESET}")
            print()
        for key, label, _fn in MODES:
            print(f"  {GREEN}{key}){RESET} {label}")
        print(f"  {GREEN}q){RESET} Quitter")
        print()
        # Default to "m" if there are instances (the most likely action), else nothing
        choice = ask("Choix", "m" if has_instances else "")
        print()
        if choice in ("q", "Q", "quit", "exit"):
            return
        if choice == "m" and has_instances:
            try:
                mode_manage()
            except KeyboardInterrupt:
                print()
                warn("Interrompu.")
            except Exception as e:
                err(f"Erreur : {e}")
            print()
            if not confirm("Retour au menu principal ?", True):
                return
            continue
        for key, _label, fn in MODES:
            if choice == key:
                try:
                    fn()
                except KeyboardInterrupt:
                    print()
                    warn("Interrompu.")
                except Exception as e:
                    err(f"Erreur : {e}")
                break
        else:
            warn(f"Choix invalide : '{choice}'")
        print()
        if not confirm("Retour au menu principal ?", True):
            return


# ── CLI shortcuts ───────────────────────────────────────────────────────────
SHORTCUTS = {
    "--manage":            mode_manage,
    "--single":            mode_single,
    "--multi":             mode_multi_instance,
    "--cluster-shared":    mode_cluster_shared,
    "--cluster-pool":      mode_cluster_pool,
    "--pool-only":         mode_pool_only,
    "--status":            mode_status,
    "--parallel-download": mode_parallel_download,
    "--stop":              mode_stop,
}


def main() -> None:
    args = sys.argv[1:]
    if not args:
        main_menu()
        return
    a = args[0]
    if a in ("-h", "--help"):
        print(__doc__)
        return
    fn = SHORTCUTS.get(a)
    if fn is None:
        err(f"Argument inconnu : {a}")
        print("Disponibles :", ", ".join(SHORTCUTS), "(ou aucun pour le menu)")
        sys.exit(1)
    fn()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
        warn("Interrompu.")
        sys.exit(130)
