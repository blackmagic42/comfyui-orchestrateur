#!/usr/bin/env python3
"""
setup.py — Cross-platform installer for the ComfyUI Orchestrator.

The orchestrator is a small Python service. Once it's running, you do
**everything else** from its web dashboard at http://127.0.0.1:9000/dashboard :
  - Pick a model budget (250 / 400 / 700 / 1500 GB or custom slider)
  - Click "Install ComfyUI" to deploy a fresh instance
  - Click "Apply changes" to build + download + cleanup the catalog
  - Manage running instances (start, stop, status)
  - Submit workflow jobs that get routed to a healthy ComfyUI

This script does NOT install ComfyUI itself — that happens via the dashboard
once the orchestrator is up.

Modes :
  setup.py             interactive menu (default)
  setup.py --install   install + start the orchestrator
  setup.py --start     start an already-installed orchestrator
  setup.py --stop      stop the orchestrator
  setup.py --open      open the dashboard URL in your browser
  setup.py --logs      tail the orchestrator log
  setup.py --status    show install + run state

State :
  ~/.comfyui-orchestrator/         run dir (logs, pid file, config)

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

# ── stdout UTF-8 so Unicode glyphs work under Windows pipes / cmd.exe ───────
for _stream_name in ("stdout", "stderr"):
    _s = getattr(sys, _stream_name, None)
    if _s is not None and hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

SCRIPT_DIR  = Path(__file__).resolve().parent
ORCH_SCRIPT = SCRIPT_DIR / "orchestrator.py"
RUN_DIR     = Path.home() / ".comfyui-orchestrator"
PID_FILE    = RUN_DIR / "orchestrator.pid"
LOG_FILE    = RUN_DIR / "orchestrator.log"
CONFIG_FILE = RUN_DIR / "config.json"
DEFAULT_PORT = 9000

IS_WIN   = os.name == "nt"
IS_LINUX = sys.platform.startswith("linux")
IS_MAC   = sys.platform == "darwin"

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
            k32.SetConsoleMode(h, mode.value | 0x4)
    except Exception:
        pass


# ── UI helpers ──────────────────────────────────────────────────────────────
def banner() -> None:
    print(f"""
{CYAN}{BOLD}  ╔═══════════════════════════════════════════════════════════════╗
  ║         ComfyUI Orchestrator — installer & lifecycle          ║
  ╚═══════════════════════════════════════════════════════════════╝{RESET}
  {DIM}OS: {platform.system()} {platform.release()} · Python {platform.python_version()}{RESET}
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


# ── State helpers ───────────────────────────────────────────────────────────
def load_config() -> dict:
    if CONFIG_FILE.exists():
        try: return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception: pass
    return {"port": DEFAULT_PORT}

def save_config(cfg: dict) -> None:
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")

def is_port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.5) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try: return s.connect_ex((host, port)) == 0
    except Exception: return False
    finally: s.close()

def pid_alive(pid: int) -> bool:
    if not pid: return False
    if IS_WIN:
        try:
            r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                               capture_output=True, text=True, timeout=5)
            return str(pid) in r.stdout
        except Exception: return False
    try:
        os.kill(pid, 0); return True
    except (ProcessLookupError, PermissionError):
        return Path(f"/proc/{pid}").exists() if IS_LINUX else False
    except Exception:
        return False

def get_running_pid() -> int | None:
    if not PID_FILE.exists(): return None
    try: pid = int(PID_FILE.read_text().strip())
    except Exception: return None
    return pid if pid_alive(pid) else None


# ── Pré-vérifs ──────────────────────────────────────────────────────────────
def check_prereqs() -> bool:
    step("Vérification des pré-requis")
    missing = [c for c in ("python", "git") if not shutil.which(c)]
    if missing:
        err(f"Outils manquants : {', '.join(missing)}")
        return False
    ok(f"python {platform.python_version()} · git OK")
    if not ORCH_SCRIPT.exists():
        err(f"orchestrator.py introuvable à {ORCH_SCRIPT}")
        hint("Tu dois lancer setup.py depuis le dossier scripts/ du repo cloné.")
        return False
    ok(f"orchestrator.py présent ({ORCH_SCRIPT})")
    return True


# ── Modes ───────────────────────────────────────────────────────────────────
def mode_install() -> None:
    """Install + start the orchestrator. The ONLY user-facing install step.
    Everything else (deploying ComfyUI, picking a budget, managing instances)
    happens in the web dashboard.
    """
    banner()
    step(f"Installation de l'orchestrateur")
    print()
    if not check_prereqs():
        return

    cfg = load_config()
    port = ask_int("Port pour le dashboard", cfg.get("port", DEFAULT_PORT))
    if is_port_open(port):
        warn(f"Le port {port} est déjà utilisé.")
        if not confirm("Arrêter ce qui tourne dessus avant de lancer ?", False):
            return
        # try gentle stop via PID file
        existing_pid = get_running_pid()
        if existing_pid:
            stop_orchestrator()

    cfg["port"] = port
    save_config(cfg)
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    ok(f"Run dir : {RUN_DIR}")

    if confirm("Démarrer l'orchestrateur maintenant ?", True):
        if not start_orchestrator(port):
            err("Démarrage échoué — voir les logs.")
            return
        url = f"http://127.0.0.1:{port}/dashboard"
        if confirm(f"Ouvrir {url} dans le navigateur ?", True):
            try: webbrowser.open(url)
            except Exception: pass

    print()
    step(f"{GREEN}Installation terminée{RESET}")
    print()
    print(f"  Dashboard : {BOLD}http://127.0.0.1:{port}/dashboard{RESET}")
    print()
    print(f"  Depuis le dashboard, tu peux maintenant :")
    print(f"    {DIM}·{RESET} Installer ComfyUI (onglet ⚙ Commands → 'Install ComfyUI')")
    print(f"    {DIM}·{RESET} Choisir un budget de modèles (slider 100-2000 GB)")
    print(f"    {DIM}·{RESET} Apply changes → build catalog + download + cleanup")
    print(f"    {DIM}·{RESET} Soumettre des workflows qui s'exécutent sur l'instance vivante")
    print()
    print(f"  Commandes utiles :")
    print(f"    {DIM}python setup.py --start{RESET}   redémarrer l'orchestrateur")
    print(f"    {DIM}python setup.py --stop{RESET}    l'arrêter")
    print(f"    {DIM}python setup.py --logs{RESET}    voir le log")
    print(f"    {DIM}python setup.py --open{RESET}    ouvrir le dashboard")


def start_orchestrator(port: int | None = None) -> bool:
    """Spawn orchestrator.py serve as a detached process. Returns True on success."""
    cfg = load_config()
    port = port or cfg.get("port", DEFAULT_PORT)

    existing = get_running_pid()
    if existing:
        warn(f"Orchestrateur déjà en cours · PID {existing} · http://127.0.0.1:{port}/dashboard")
        return True

    RUN_DIR.mkdir(parents=True, exist_ok=True)
    log_f = open(LOG_FILE, "ab")
    log_f.write(f"\n=== boot {datetime.now().isoformat()} ===\n".encode())

    cmd = [sys.executable, str(ORCH_SCRIPT), "serve", "--port", str(port)]
    try:
        if IS_WIN:
            DETACHED = 0x00000008
            proc = subprocess.Popen(
                cmd, cwd=str(SCRIPT_DIR),
                stdin=subprocess.DEVNULL, stdout=log_f, stderr=log_f,
                creationflags=DETACHED | subprocess.CREATE_NEW_PROCESS_GROUP,
                close_fds=True,
            )
        else:
            proc = subprocess.Popen(
                cmd, cwd=str(SCRIPT_DIR),
                stdin=subprocess.DEVNULL, stdout=log_f, stderr=log_f,
                start_new_session=True, close_fds=True,
            )
    except Exception as e:
        err(f"Spawn failed : {e}")
        log_f.close()
        return False

    PID_FILE.write_text(str(proc.pid), encoding="utf-8")
    save_config({**cfg, "port": port})
    ok(f"Orchestrateur lancé · PID {proc.pid} · log : {LOG_FILE}")

    # Wait briefly for the port to come up so the user gets quick feedback
    for _ in range(20):
        time.sleep(0.5)
        if is_port_open(port):
            ok(f"Port {port} répond — http://127.0.0.1:{port}/dashboard")
            return True
    warn(f"Port {port} ne répond pas encore (boot en cours, voir le log).")
    return True


def stop_orchestrator() -> bool:
    pid = get_running_pid()
    if not pid:
        warn("Aucun orchestrateur en cours.")
        if PID_FILE.exists(): PID_FILE.unlink()
        return False
    try:
        if IS_WIN:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                            capture_output=True, timeout=10)
        else:
            try: os.kill(pid, signal.SIGTERM)
            except ProcessLookupError: pass
            for _ in range(10):
                if not pid_alive(pid): break
                time.sleep(0.3)
            if pid_alive(pid):
                try: os.kill(pid, signal.SIGKILL)
                except ProcessLookupError: pass
    except Exception as e:
        warn(f"Échec kill PID {pid} : {e}")
        return False
    if PID_FILE.exists(): PID_FILE.unlink()
    ok(f"Orchestrateur arrêté (PID {pid})")
    return True


def open_dashboard() -> None:
    cfg = load_config()
    port = cfg.get("port", DEFAULT_PORT)
    url = f"http://127.0.0.1:{port}/dashboard"
    if not is_port_open(port):
        warn(f"Port {port} fermé — l'orchestrateur ne tourne pas.")
        if confirm("Le démarrer maintenant ?", True):
            if not start_orchestrator(port):
                return
        else:
            return
    try:
        webbrowser.open(url)
        ok(f"Ouvert : {url}")
    except Exception:
        hint(f"Va manuellement sur {url}")


def tail_log(n: int = 80) -> None:
    if not LOG_FILE.exists():
        warn(f"Pas de log à {LOG_FILE}")
        return
    try:
        with LOG_FILE.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 64 * 1024))
            chunk = f.read().decode("utf-8", errors="replace")
        lines = chunk.splitlines()[-n:]
        print(f"{DIM}── tail -{n} {LOG_FILE} ──{RESET}")
        for line in lines: print(line)
        print(f"{DIM}── end ──{RESET}")
    except Exception as e:
        err(f"Lecture log : {e}")


def show_status() -> None:
    banner()
    cfg = load_config()
    port = cfg.get("port", DEFAULT_PORT)
    pid = get_running_pid()

    print(f"  Run dir       : {RUN_DIR}")
    print(f"  Port           : {port}")
    print(f"  PID file       : {PID_FILE} {'(present)' if PID_FILE.exists() else '(absent)'}")
    print(f"  Process alive  : {GREEN+'oui · PID '+str(pid)+RESET if pid else DIM+'non'+RESET}")
    print(f"  Port {port} open  : {GREEN+'oui'+RESET if is_port_open(port) else DIM+'non'+RESET}")
    print(f"  Log            : {LOG_FILE} ({LOG_FILE.stat().st_size if LOG_FILE.exists() else 0} bytes)")
    print()
    if pid and is_port_open(port):
        print(f"  {GREEN}● Orchestrateur en cours{RESET}")
        print(f"    Dashboard : {BOLD}http://127.0.0.1:{port}/dashboard{RESET}")
    else:
        print(f"  {DIM}○ Orchestrateur arrêté{RESET}")
        print(f"    Lance : {DIM}python setup.py --start{RESET}")


# ── Main menu ───────────────────────────────────────────────────────────────
def main_menu() -> None:
    while True:
        banner()
        cfg = load_config()
        port = cfg.get("port", DEFAULT_PORT)
        pid = get_running_pid()
        running = bool(pid and is_port_open(port))

        if running:
            print(f"  {GREEN}● Orchestrateur en cours{RESET} · PID {pid} · port {port}")
            print(f"    Dashboard : {BOLD}http://127.0.0.1:{port}/dashboard{RESET}")
        elif PID_FILE.exists() or LOG_FILE.exists():
            print(f"  {DIM}○ Orchestrateur installé mais arrêté{RESET}")
        else:
            print(f"  {DIM}∅ Pas encore installé{RESET}")
        print()

        print(f"{BOLD}Actions :{RESET}\n")
        if running:
            print(f"  {GREEN}o){RESET} Ouvrir le dashboard dans le navigateur")
            print(f"  {GREEN}s){RESET} Arrêter l'orchestrateur")
            print(f"  {GREEN}r){RESET} Redémarrer l'orchestrateur")
        else:
            print(f"  {GREEN}i){RESET} {BOLD}Installer / démarrer{RESET} l'orchestrateur")
        print(f"  {GREEN}l){RESET} Voir le log")
        print(f"  {GREEN}t){RESET} Status détaillé")
        print(f"  {GREEN}q){RESET} Quitter")
        print()

        default = "o" if running else "i"
        choice = ask("Choix", default).lower()
        print()

        if choice in ("q", "quit", "exit"): return
        if choice == "i": mode_install()
        elif choice == "o": open_dashboard()
        elif choice == "s":
            if confirm("Confirmer l'arrêt ?", True): stop_orchestrator()
        elif choice == "r":
            stop_orchestrator()
            time.sleep(1)
            start_orchestrator(port)
        elif choice == "l": tail_log()
        elif choice == "t": show_status()
        else: warn(f"Choix invalide : '{choice}'")

        print()
        if not confirm("Retour au menu principal ?", True):
            return


# ── CLI ─────────────────────────────────────────────────────────────────────
def main() -> None:
    args = sys.argv[1:]
    if not args:
        main_menu(); return
    a = args[0]
    if a in ("-h", "--help"):
        print(__doc__); return
    if a == "--install": mode_install()
    elif a == "--start":
        if not start_orchestrator(): sys.exit(1)
    elif a == "--stop":
        if not stop_orchestrator(): sys.exit(1)
    elif a == "--open": open_dashboard()
    elif a == "--logs": tail_log()
    elif a == "--status": show_status()
    else:
        err(f"Argument inconnu : {a}")
        print("Disponibles : --install, --start, --stop, --open, --logs, --status, --help")
        sys.exit(1)


if __name__ == "__main__":
    try: main()
    except KeyboardInterrupt:
        print(); warn("Interrompu."); sys.exit(130)
