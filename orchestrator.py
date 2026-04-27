#!/usr/bin/env python
"""
Orchestrateur ComfyUI — proxy/load-balancer entre un VPS et N instances ComfyUI.

Fonctionnalités :
  • Détecte les instances ComfyUI vivantes (scan des ports 8188-8192)
  • Auto-launch d'une instance si aucune n'est dispo
  • Distribue les jobs de test sur les instances en round-robin
  • Persist les résultats (status, durée, output, erreur)
  • Sert un dashboard HTML qui affiche l'état des workflows par catégorie
  • Bonus : skip auto les workflows déjà marqués "ok" — pas besoin de re-tester

Usage :
    python orchestrator.py serve --port 9000        # API + dashboard
    python orchestrator.py test --phase 1           # Lancer les tests phase 1
    python orchestrator.py launch                   # Démarrer une instance ComfyUI
    python orchestrator.py status                   # Voir les instances vivantes
"""
from __future__ import annotations
import sys
sys.stdout.reconfigure(encoding="utf-8")

import argparse
import http.server
import json
import os
import socket
import subprocess
import sys
import time
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path

STATE_DIR = Path(os.environ.get("COMFYUI_STATE_DIR",
    str(Path(__file__).resolve().parent.parent / ".catalog_state")))
STATE_DIR.mkdir(exist_ok=True)
RESULTS_FILE = STATE_DIR / "test_results.json"
INSTANCES_FILE = STATE_DIR / "instances.json"
API_DIR = STATE_DIR / "api_workflows"

DEFAULT_COMFYUI_PATH = Path(os.environ.get("COMFYUI_PATH",
    str(Path(__file__).resolve().parent.parent / "ComfyUI")))
DEFAULT_PORTS = list(range(8188, 8193))

# ── Auth ───────────────────────────────────────────────────────────────────
# Bearer token requis pour les endpoints sensibles (/api/*).
# Set via env ORCHESTRATOR_TOKEN; defaults to a generated one written to disk.
TOKEN_FILE = STATE_DIR / "auth_token"
def get_or_create_token() -> str:
    env = os.environ.get("ORCHESTRATOR_TOKEN")
    if env:
        return env
    if TOKEN_FILE.exists():
        return TOKEN_FILE.read_text(encoding="utf-8").strip()
    import secrets
    tok = secrets.token_urlsafe(32)
    TOKEN_FILE.write_text(tok, encoding="utf-8")
    try: os.chmod(TOKEN_FILE, 0o600)
    except Exception: pass
    return tok

# Track active jobs in-memory (persisted to RESULTS_FILE on completion)
ACTIVE_JOBS: dict[str, dict] = {}
ACTIVE_JOBS_LOCK = threading.Lock()


# ── Instance discovery ──────────────────────────────────────────────────────

def is_port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            return s.connect_ex((host, port)) == 0
    except Exception:
        return False


def health_check(base_url: str, timeout: float = 2.0) -> dict | None:
    """Returns {alive: True, queue_size: N, version: ...} or None."""
    try:
        with urllib.request.urlopen(f"{base_url}/queue", timeout=timeout) as resp:
            data = json.loads(resp.read())
        running = len(data.get("queue_running") or [])
        pending = len(data.get("queue_pending") or [])
        return {"alive": True, "running": running, "pending": pending,
                "load": running + pending}
    except Exception:
        return None


def discover_instances(host: str = "127.0.0.1", ports=None) -> list[dict]:
    """Returns list of alive ComfyUI instances with their load."""
    ports = ports or DEFAULT_PORTS
    found = []
    for p in ports:
        if not is_port_open(host, p):
            continue
        info = health_check(f"http://{host}:{p}")
        if info:
            found.append({"host": host, "port": p, **info,
                           "url": f"http://{host}:{p}"})
    return found


def launch_comfyui(install_path: Path = DEFAULT_COMFYUI_PATH,
                    port: int = 8188,
                    extra_args: list[str] = None) -> int:
    """Launch ComfyUI in background. Returns the launched process PID."""
    cmd = [
        sys.executable, str(install_path / "main.py"),
        "--listen", "0.0.0.0",
        "--port", str(port),
        "--disable-xformers",
        "--use-pytorch-cross-attention",
    ]
    if extra_args:
        cmd.extend(extra_args)

    log_file = STATE_DIR / f"comfyui_{port}.log"
    log_handle = open(log_file, "w", encoding="utf-8")
    if os.name == "nt":
        proc = subprocess.Popen(
            cmd, cwd=str(install_path),
            stdout=log_handle, stderr=subprocess.STDOUT,
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
        )
    else:
        proc = subprocess.Popen(
            cmd, cwd=str(install_path),
            stdout=log_handle, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    return proc.pid


def wait_alive(url: str, timeout: int = 60) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        if health_check(url):
            return True
        time.sleep(2)
    return False


# ── Result persistence ──────────────────────────────────────────────────────

def load_results() -> dict:
    if RESULTS_FILE.exists():
        return json.loads(RESULTS_FILE.read_text(encoding="utf-8"))
    return {"runs": [], "by_workflow": {}}


def save_results(results: dict):
    RESULTS_FILE.write_text(json.dumps(results, indent=2), encoding="utf-8")


def load_classes() -> dict:
    f = STATE_DIR / "workflow_classes.json"
    if not f.exists():
        return {}
    return json.loads(f.read_text(encoding="utf-8"))


# ── Job dispatch ────────────────────────────────────────────────────────────

def submit_job(instance: dict, api_graph: dict, client_id: str) -> str | None:
    """Returns prompt_id, or None on error."""
    body = json.dumps({"prompt": api_graph, "client_id": client_id}).encode()
    req = urllib.request.Request(
        f"{instance['url']}/prompt",
        data=body, headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())["prompt_id"]
    except Exception:
        return None


def poll_history(instance: dict, prompt_id: str, timeout: int = 600) -> dict:
    """Wait for prompt to complete; returns the history entry."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with urllib.request.urlopen(
                f"{instance['url']}/history/{prompt_id}", timeout=10
            ) as resp:
                h = json.loads(resp.read())
            if prompt_id in h:
                return h[prompt_id]
        except Exception:
            pass
        time.sleep(2)
    return {}


def pick_least_loaded(instances: list[dict]) -> dict | None:
    if not instances:
        return None
    return min(instances, key=lambda i: i.get("load", 0))


# ── Subcommand: test ────────────────────────────────────────────────────────

def cmd_test(args):
    """Distribute test jobs across alive instances."""
    instances = discover_instances()
    if not instances:
        print("Aucune instance ComfyUI vivante.")
        if args.auto_launch:
            print("Lancement automatique sur 8188...")
            launch_comfyui(port=8188)
            if wait_alive("http://127.0.0.1:8188", timeout=120):
                instances = discover_instances()
            else:
                print("Échec du démarrage", file=sys.stderr)
                sys.exit(1)
        else:
            sys.exit(1)
    print(f"🟢 {len(instances)} instance(s) trouvée(s) :")
    for i in instances:
        print(f"     {i['url']}  load={i.get('load',0)}")

    # Load classifier + select targets
    classes = load_classes()
    if not classes:
        print("⚠ workflow_classes.json absent — lance classify_workflows.py", file=sys.stderr)
        sys.exit(1)

    results = load_results()
    targets = []
    phases = list(range(1, 11)) if args.all else [args.phase]
    for tid, k in classes.items():
        if k.get("phase") not in phases:
            continue
        if args.skip_done and tid in results["by_workflow"] \
           and results["by_workflow"][tid].get("status") == "ok":
            continue
        # Ensure API workflow exported
        api_path = API_DIR / f"{tid}.json"
        if not api_path.exists():
            continue
        targets.append((tid, k, api_path))

    if args.limit:
        targets = targets[:args.limit]
    if not targets:
        print("Aucun workflow à tester.")
        return

    print(f"📤 Distribution de {len(targets)} workflows sur {len(instances)} instance(s)")
    client_id = uuid.uuid4().hex
    run_id = datetime.now().isoformat()
    pending = list(targets)

    # Simple dispatch loop : prend la moins chargée pour chaque job
    completed = 0
    while pending:
        instances = discover_instances()  # refresh
        instance = pick_least_loaded(instances)
        if not instance:
            print("⚠ Aucune instance dispo, attente 5s...")
            time.sleep(5)
            continue

        tid, klass, api_path = pending.pop(0)
        print(f"  → [{completed+1}/{len(targets)}] {tid} → {instance['url']}")
        try:
            api_graph = json.loads(api_path.read_text(encoding="utf-8"))
            t0 = time.time()
            prompt_id = submit_job(instance, api_graph, client_id)
            if not prompt_id:
                results["by_workflow"][tid] = {
                    "status": "error", "phase": klass.get("phase"),
                    "category": klass.get("category"),
                    "instance": instance["url"],
                    "error": "submit failed", "duration": 0,
                    "last_run": run_id,
                }
                completed += 1
                save_results(results)
                continue

            entry = poll_history(instance, prompt_id, timeout=args.timeout)
            duration = time.time() - t0
            outputs = entry.get("outputs", {})
            status_obj = entry.get("status", {})
            output_files = []
            for _id, out in outputs.items():
                for kind in ("images", "gifs", "videos", "audio"):
                    for f in (out.get(kind) or []):
                        if isinstance(f, dict) and f.get("filename"):
                            output_files.append({
                                "kind": kind, "filename": f["filename"],
                                "subfolder": f.get("subfolder", ""),
                            })

            if status_obj.get("status_str") == "success" or output_files:
                status = "ok"
                err = ""
            elif status_obj.get("status_str") == "error":
                status = "error"
                msgs = status_obj.get("messages", [])
                err = "; ".join(str(m) for m in msgs)[:300]
            elif not entry:
                status = "timeout"
                err = "no history entry after timeout"
            else:
                status = "unknown"
                err = ""

            results["by_workflow"][tid] = {
                "status": status, "phase": klass.get("phase"),
                "category": klass.get("category"),
                "instance": instance["url"],
                "duration": round(duration, 1),
                "outputs": output_files,
                "error": err,
                "prompt_id": prompt_id,
                "last_run": run_id,
            }
            completed += 1
            save_results(results)
        except Exception as exc:
            results["by_workflow"][tid] = {
                "status": "error", "phase": klass.get("phase"),
                "category": klass.get("category"),
                "instance": instance["url"],
                "error": f"exception: {exc}", "duration": 0,
                "last_run": run_id,
            }
            completed += 1
            save_results(results)

    print(f"\n📊 Terminé : {completed} workflows testés sur {len(targets)} cibles")


# ── Subcommand: launch ──────────────────────────────────────────────────────

def cmd_launch(args):
    pid = launch_comfyui(install_path=args.install_path, port=args.port)
    print(f"🚀 ComfyUI lancé (PID={pid}) sur port {args.port}")
    print(f"   Log : {STATE_DIR / f'comfyui_{args.port}.log'}")
    if args.wait:
        if wait_alive(f"http://127.0.0.1:{args.port}", timeout=120):
            print(f"   ✅ Vivant")
        else:
            print(f"   ⏱  Timeout")


def cmd_status(args):
    instances = discover_instances()
    if not instances:
        print("Aucune instance vivante")
        return
    for i in instances:
        print(f"  {i['url']:30}  running={i['running']:>2} pending={i['pending']:>2}")

    if RESULTS_FILE.exists():
        results = load_results()
        ok = sum(1 for v in results["by_workflow"].values() if v.get("status") == "ok")
        err = sum(1 for v in results["by_workflow"].values() if v.get("status") == "error")
        to = sum(1 for v in results["by_workflow"].values() if v.get("status") == "timeout")
        print(f"\nRésultats persistés : {ok} OK · {err} errors · {to} timeouts")


# ── Subcommand: serve (dashboard + API) ────────────────────────────────────


_instances_cache = {"data": [], "ts": 0}
_INSTANCES_CACHE_TTL = 5  # seconds


def discover_instances_cached() -> list[dict]:
    """Cached version — avoids blocking the dashboard handler on slow scans."""
    now = time.time()
    if now - _instances_cache["ts"] < _INSTANCES_CACHE_TTL:
        return _instances_cache["data"]
    # Fast scan: smaller timeout
    found = []
    for port in DEFAULT_PORTS:
        if not is_port_open("127.0.0.1", port, timeout=0.2):
            continue
        info = health_check(f"http://127.0.0.1:{port}", timeout=1.0)
        if info:
            found.append({"host": "127.0.0.1", "port": port, **info,
                           "url": f"http://127.0.0.1:{port}"})
    _instances_cache["data"] = found
    _instances_cache["ts"] = now
    return found


# ── Background job submission ──────────────────────────────────────────────

def submit_job_async(template_id: str, klass: dict, prompt: str | None,
                     instance: dict, results: dict, run_id: str):
    """Submit a job in a background thread and update results when done."""
    api_path = API_DIR / f"{template_id}.json"
    if not api_path.exists():
        with ACTIVE_JOBS_LOCK:
            ACTIVE_JOBS.pop(template_id, None)
        results["by_workflow"][template_id] = {
            "status": "error", "phase": klass.get("phase"),
            "category": klass.get("category"),
            "error": "API workflow not exported", "duration": 0,
            "last_run": run_id,
        }
        save_results(results)
        return

    api_graph = json.loads(api_path.read_text(encoding="utf-8"))
    # Inject prompt if provided
    if prompt:
        for nid, node in api_graph.items():
            if isinstance(node, dict) and node.get("class_type") in (
                "CLIPTextEncode", "CLIPTextEncodeSDXL", "T5TextEncode",
            ):
                node.setdefault("inputs", {})["text"] = prompt

    client_id = uuid.uuid4().hex
    t0 = time.time()
    try:
        prompt_id = submit_job(instance, api_graph, client_id)
        if not prompt_id:
            raise RuntimeError("submit failed")

        with ACTIVE_JOBS_LOCK:
            ACTIVE_JOBS[template_id] = {
                "status": "running",
                "instance": instance["url"],
                "prompt_id": prompt_id,
                "started_at": t0,
            }

        entry = poll_history(instance, prompt_id, timeout=600)
        duration = time.time() - t0
        outputs = entry.get("outputs", {})
        status_obj = entry.get("status", {})
        output_files = []
        for _id, out in outputs.items():
            for kind in ("images", "gifs", "videos", "audio"):
                for f in (out.get(kind) or []):
                    if isinstance(f, dict) and f.get("filename"):
                        output_files.append({
                            "kind": kind, "filename": f["filename"],
                            "subfolder": f.get("subfolder", ""),
                        })

        if status_obj.get("status_str") == "success" or output_files:
            status, err = "ok", ""
        elif status_obj.get("status_str") == "error":
            status = "error"
            err = "; ".join(str(m) for m in (status_obj.get("messages") or []))[:400]
        elif not entry:
            status, err = "timeout", "no history after 600s"
        else:
            status, err = "unknown", ""

        results["by_workflow"][template_id] = {
            "status": status, "phase": klass.get("phase"),
            "category": klass.get("category"),
            "instance": instance["url"],
            "duration": round(duration, 1),
            "outputs": output_files, "error": err,
            "prompt_id": prompt_id, "last_run": run_id,
        }
    except Exception as exc:
        results["by_workflow"][template_id] = {
            "status": "error", "phase": klass.get("phase"),
            "category": klass.get("category"),
            "instance": instance.get("url", "?"),
            "error": f"exception: {exc}", "duration": time.time() - t0,
            "last_run": run_id,
        }
    finally:
        with ACTIVE_JOBS_LOCK:
            ACTIVE_JOBS.pop(template_id, None)
        save_results(results)


def make_dashboard_data() -> dict:
    classes = load_classes()
    results = load_results()
    instances = discover_instances_cached()
    # Merge in-flight jobs into the workflow map so the UI shows "running"
    with ACTIVE_JOBS_LOCK:
        active = dict(ACTIVE_JOBS)

    by_category = {}
    stats = {"total": 0, "ok": 0, "error": 0, "timeout": 0, "pending": 0, "running": 0}
    for tid, k in classes.items():
        cat = k.get("category", "general")
        r = results["by_workflow"].get(tid, {})
        # Active jobs override historical status
        if tid in active:
            status = "running"
            instance = active[tid].get("instance")
            duration = round(time.time() - active[tid].get("started_at", time.time()), 1)
            err = None
        else:
            status = r.get("status") or "pending"
            if status not in ("ok", "error", "timeout"):
                status = "pending"
            instance = r.get("instance")
            duration = r.get("duration")
            err = r.get("error")

        wf = {"tid": tid, "phase": k.get("phase"), "status": status,
              "duration": duration, "instance": instance, "error": err}
        by_category.setdefault(cat, []).append(wf)
        stats["total"] += 1
        stats[status] = stats.get(status, 0) + 1

    groups = []
    for cat, items in sorted(by_category.items()):
        ok = sum(1 for w in items if w["status"] == "ok")
        err = sum(1 for w in items if w["status"] == "error")
        to = sum(1 for w in items if w["status"] == "timeout")
        pn = sum(1 for w in items if w["status"] == "pending")
        groups.append({
            "name": cat, "workflows": items,
            "ok": ok, "error": err, "timeout": to, "pending": pn,
        })

    return {"stats": stats, "instances": instances, "groups": groups}


# ── Whitelisted commands (executed on the host) ─────────────────────────────
SCRIPTS_DIR = Path(__file__).parent
COMMAND_WHITELIST = {
    "catalog_build": {
        "label": "📦 Build catalog",
        "description": "Construit le manifest (latest + biggest variant)",
        "cmd": [sys.executable, str(SCRIPTS_DIR / "comfyui_catalog.py"), "build"],
        "params": [
            {"name": "budget",         "flag": "--budget",         "type": "int",
             "default": 700,           "label": "Budget (GB)"},
            {"name": "max_age_years",  "flag": "--max-age-years",  "type": "int",
             "default": 2,             "label": "Âge max (ans)"},
        ],
    },
    "catalog_sync": {
        "label": "🔄 Sync (fetch + rebuild)",
        "description": "pip install -U comfyui_workflow_templates puis rebuild du manifest. À lancer périodiquement pour récupérer les nouveaux workflows et modèles.",
        "cmd": [sys.executable, str(SCRIPTS_DIR / "comfyui_catalog.py"), "sync"],
        "params": [
            {"name": "budget",         "flag": "--budget",         "type": "int",
             "default": 700,           "label": "Budget (GB)"},
            {"name": "max_age_years",  "flag": "--max-age-years",  "type": "int",
             "default": 2,             "label": "Âge max (ans)"},
        ],
    },
    "catalog_cleanup": {
        "label": "🗑 Cleanup obsolete models",
        "description": "Supprime les modèles sur disque qui ne sont plus dans le manifest (récupère de l'espace après un rebuild)",
        "cmd": [sys.executable, str(SCRIPTS_DIR / "comfyui_catalog.py"), "cleanup", "--yes"],
        "params": [],
    },
    "catalog_download": {
        "label": "📥 Download models",
        "description": "Télécharge les modèles du manifest via l'extension",
        "cmd": [sys.executable, str(SCRIPTS_DIR / "comfyui_catalog.py"), "download"],
        "params": [],
    },
    "catalog_status": {
        "label": "📊 Catalog report",
        "description": "Affiche le rapport du manifest courant",
        "cmd": [sys.executable, str(SCRIPTS_DIR / "comfyui_catalog.py"), "report"],
        "params": [],
    },
    "classify": {
        "label": "🏷️ Classify workflows",
        "description": "Classifie les 218 workflows en 10 phases × 19 catégories",
        "cmd": [sys.executable, str(SCRIPTS_DIR / "classify_workflows.py")],
        "params": [],
    },
    "export_api": {
        "label": "🔄 Export workflows → API",
        "description": "Convertit les workflows UI en format API (pour /prompt)",
        "cmd": [sys.executable, str(SCRIPTS_DIR / "export_workflows_api.py")],
        "params": [],
    },
    "starters": {
        "label": "🌱 Generate starter images",
        "description": "Génère 14 images contextuelles par catégorie",
        "cmd": [sys.executable, str(SCRIPTS_DIR / "generate_starters.py")],
        "params": [],
    },
    "gated_check": {
        "label": "🔐 Check gated HF models",
        "description": "Identifie les modèles HuggingFace nécessitant licence",
        "cmd": [sys.executable, str(SCRIPTS_DIR / "check_gated_models.py")],
        "params": [],
    },
    "install_workflows": {
        "label": "📁 Install workflows in ComfyUI",
        "description": "Copie les 218 workflows dans user/default/workflows/",
        "cmd": [sys.executable, str(SCRIPTS_DIR / "comfyui_catalog.py"), "install-workflows"],
        "params": [],
    },
    "launch_comfyui": {
        "label": "🚀 Launch ComfyUI instance",
        "description": "Démarre une nouvelle instance sur le port donné",
        "cmd": [sys.executable, str(SCRIPTS_DIR / "orchestrator.py"), "launch"],
        "params": [
            {"name": "port", "flag": "--port", "type": "int",
             "default": 8188, "label": "Port"},
        ],
    },

    # ── First-time setup ───────────────────────────────────────────────
    # OS-aware: on Windows we route to the .ps1, elsewhere to the .sh.
    # The dashboard form lets the user override install_path / port / cuda.
    "bootstrap_install": (
        {
            "label": "⚙ First-time install ComfyUI (Windows)",
            "description": "Installe ComfyUI from scratch : git clone + venv + torch CUDA + custom nodes + firewall. Skippe le download des modèles (à faire ensuite via Apply changes).",
            "cmd": ["pwsh", "-NoProfile", "-ExecutionPolicy", "Bypass",
                    "-File", str(SCRIPTS_DIR / "install_comfyui.ps1"),
                    "-SkipModels", "-SkipWorkflows"],
            "params": [
                {"name": "install_path", "flag": "-InstallPath", "type": "str",
                 "default": str(Path.home() / "ComfyUI"), "label": "Chemin d'install"},
                {"name": "budget",       "flag": "-Budget",      "type": "int",
                 "default": 0,           "label": "Budget (GB, 0 = skip)"},
            ],
        } if os.name == "nt" else
        {
            "label": "⚙ First-time install ComfyUI",
            "description": "Installe ComfyUI from scratch : git clone + venv + torch CUDA + custom nodes + firewall. Skippe le download des modèles (à faire ensuite via Apply changes).",
            "cmd": ["bash", str(SCRIPTS_DIR / "install_comfyui.sh"),
                    "--skip-models", "--skip-workflows"],
            "params": [
                {"name": "install_path", "flag": "--install-path", "type": "str",
                 "default": str(Path.home() / "comfyui"), "label": "Chemin d'install"},
                {"name": "budget",       "flag": "--budget",       "type": "int",
                 "default": 0,           "label": "Budget (GB, 0 = skip)"},
            ],
        }
    ),

    # ── Bundle presets (raccourcis budget+thème) ──────────────────────
    "bundle_minimal": {
        "label": "📦 Bundle: Minimal (250 GB)",
        "description": "Catalogue minimal — text→image essentials only. Idéal pour première installation rapide ou stockage limité.",
        "cmd": [sys.executable, str(SCRIPTS_DIR / "comfyui_catalog.py"), "build",
                "--budget", "250", "--max-age-years", "2"],
        "params": [],
    },
    "bundle_image": {
        "label": "🖼 Bundle: Image-focused (400 GB)",
        "description": "Image generation + edit complet (Flux, Qwen-Edit, HiDream, ControlNet) sans video.",
        "cmd": [sys.executable, str(SCRIPTS_DIR / "comfyui_catalog.py"), "build",
                "--budget", "400", "--max-age-years", "2"],
        "params": [],
    },
    "bundle_standard": {
        "label": "🎬 Bundle: Standard (700 GB)",
        "description": "Recommandé : image + video latest (Wan2.2, LTX 2.3, Hunyuan Video 1.5).",
        "cmd": [sys.executable, str(SCRIPTS_DIR / "comfyui_catalog.py"), "build",
                "--budget", "700", "--max-age-years", "2"],
        "params": [],
    },
    "bundle_full": {
        "label": "🎯 Bundle: Full catalog (1500 GB)",
        "description": "Toutes les latest versions sans contrainte budget — pour station haut de gamme.",
        "cmd": [sys.executable, str(SCRIPTS_DIR / "comfyui_catalog.py"), "build",
                "--budget", "1500", "--max-age-years", "2"],
        "params": [],
    },

    # ── Macro: apply changes ─────────────────────────────────────────────
    "apply_changes": {
        "label": "✨ Apply changes (build + download + cleanup)",
        "description": "Macro complète : rebuild manifest avec nouveau budget → télécharge les nouveaux modèles → supprime les obsolètes. À utiliser après un changement de bundle ou de budget.",
        "cmd": [sys.executable, str(SCRIPTS_DIR / "comfyui_catalog.py"), "apply"],
        "params": [
            {"name": "budget", "flag": "--budget", "type": "int",
             "default": 700, "label": "Budget (GB)"},
            {"name": "max_age_years", "flag": "--max-age-years", "type": "int",
             "default": 2, "label": "Âge max (ans)"},
        ],
    },
}


# Track running command processes
COMMAND_PROCESSES: dict[str, dict] = {}
COMMAND_LOCK = threading.Lock()


def run_command_async(cmd_id: str, cmd: list[str], extra_args: list[str]):
    """Run a command in a thread, capture stdout, persist log."""
    full_cmd = cmd + extra_args
    log_file = STATE_DIR / f"cmd_{cmd_id}_{int(time.time())}.log"
    log_handle = open(log_file, "w", encoding="utf-8")
    try:
        proc = subprocess.Popen(
            full_cmd,
            stdout=log_handle, stderr=subprocess.STDOUT,
            cwd=str(Path(__file__).parent.parent),
            text=True, encoding="utf-8",
        )
        with COMMAND_LOCK:
            COMMAND_PROCESSES[cmd_id] = {
                "pid": proc.pid, "log": str(log_file),
                "started_at": time.time(),
                "cmd": " ".join(full_cmd),
                "status": "running",
            }
        rc = proc.wait()
        with COMMAND_LOCK:
            COMMAND_PROCESSES[cmd_id]["status"] = "ok" if rc == 0 else "error"
            COMMAND_PROCESSES[cmd_id]["return_code"] = rc
            COMMAND_PROCESSES[cmd_id]["finished_at"] = time.time()
    except Exception as exc:
        with COMMAND_LOCK:
            COMMAND_PROCESSES[cmd_id] = {
                "status": "error", "error": str(exc),
                "started_at": time.time(),
            }
    finally:
        log_handle.close()


def get_workflow_details(template_id: str) -> dict:
    """Returns full workflow info: classification, default prompts, API graph,
    last run result, output paths."""
    classes = load_classes()
    if template_id not in classes:
        return {"error": "unknown template"}

    klass = classes[template_id]
    results = load_results()
    last = results["by_workflow"].get(template_id, {})

    api_graph = None
    api_path = API_DIR / f"{template_id}.json"
    if api_path.exists():
        try:
            api_graph = json.loads(api_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    # Extract widget overview (samplers, prompts) from the API graph
    widgets_overview = []
    if api_graph:
        for nid, node in api_graph.items():
            if not isinstance(node, dict): continue
            ct = node.get("class_type", "")
            ws = node.get("_meta", {}).get("_widgets", []) or []
            if ws and ct in (
                "KSampler", "KSamplerAdvanced", "CLIPTextEncode",
                "CLIPTextEncodeSDXL", "T5TextEncode", "EmptyLatentImage",
                "EmptySD3LatentImage", "EmptyLatentVideo",
            ):
                widgets_overview.append({
                    "node_id": nid, "type": ct,
                    "widgets": ws if isinstance(ws, list) else [],
                })

    return {
        "template_id": template_id,
        "category": klass.get("category"),
        "phase": klass.get("phase"),
        "inputs": klass.get("inputs"),
        "outputs": klass.get("outputs"),
        "default_prompts": klass.get("default_prompts", []),
        "node_count": klass.get("node_count"),
        "node_types": klass.get("node_types", {}),
        "has_lora": klass.get("has_lora"),
        "has_controlnet": klass.get("has_controlnet"),
        "has_ipadapter": klass.get("has_ipadapter"),
        "last_run": last,
        "api_available": api_graph is not None,
        "api_graph": api_graph,
        "widgets_overview": widgets_overview,
    }


DASHBOARD_HTML_PATH = Path(__file__).parent / "dashboard" / "index.html"


def _read_dashboard_html() -> bytes:
    if DASHBOARD_HTML_PATH.exists():
        return DASHBOARD_HTML_PATH.read_bytes()
    return b"<h1>Dashboard HTML missing</h1>"


def _send_json(handler, data, status=200):
    payload = json.dumps(data).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(payload)))
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
    handler.end_headers()
    handler.wfile.write(payload)


class DashboardHandler(http.server.BaseHTTPRequestHandler):
    expected_token = ""  # set by serve()

    def log_message(self, *args, **kwargs):
        pass  # quiet

    def _check_auth(self) -> bool:
        if not self.expected_token:
            return True  # auth disabled
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return False
        return auth[7:].strip() == self.expected_token

    def _json(self, data, status=200):
        _send_json(self, data, status)

    def _read_body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            return json.loads(raw or b"{}")
        except Exception:
            return {}

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.end_headers()

    def do_GET(self):
        # Public: dashboard HTML
        if self.path in ("/", "/dashboard"):
            html = _read_dashboard_html()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)
            return

        # Auth-protected APIs
        if self.path.startswith("/api/"):
            if not self._check_auth():
                return self._json({"error": "auth required"}, status=401)

            if self.path == "/api/dashboard":
                return self._json(make_dashboard_data())
            if self.path == "/api/instances":
                return self._json({"instances": discover_instances_cached()})
            if self.path == "/api/jobs":
                with ACTIVE_JOBS_LOCK:
                    return self._json({"active": dict(ACTIVE_JOBS)})
            # Bundle / budget preview — what would `build --budget X` select?
            if self.path.startswith("/api/preview"):
                qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                try:
                    budget = int(qs.get("budget", ["700"])[0])
                    max_age = int(qs.get("max_age_years", ["2"])[0])
                except ValueError:
                    return self._json({"error": "invalid params"}, status=400)
                # Lazy-import to avoid circular
                sys.path.insert(0, str(SCRIPTS_DIR))
                try:
                    from comfyui_catalog import simulate_selection
                    return self._json(simulate_selection(budget, max_age))
                except Exception as exc:
                    return self._json({"error": str(exc)}, status=500)

            if self.path == "/api/setup":
                # First-time setup detection
                comfyui_present = (DEFAULT_COMFYUI_PATH / "main.py").exists()
                manifest_present = (STATE_DIR / "manifest.json").exists()
                classes_present = (STATE_DIR / "workflow_classes.json").exists()
                api_workflows_present = API_DIR.exists() and len(list(API_DIR.glob("*.json"))) > 0
                instances = discover_instances_cached()
                manifest_info = {}
                if manifest_present:
                    try:
                        m = json.loads((STATE_DIR / "manifest.json").read_text(encoding="utf-8"))
                        manifest_info = {
                            "budget_gb": m.get("budget_gb"),
                            "selection_count": m.get("selection_count"),
                            "selection_size_gb": round(m.get("selection_size_bytes", 0) / 1024**3, 1),
                            "generated_at": m.get("generated_at"),
                        }
                    except Exception:
                        pass
                return self._json({
                    "comfyui_installed": comfyui_present,
                    "comfyui_path": str(DEFAULT_COMFYUI_PATH),
                    "manifest_built": manifest_present,
                    "classes_built": classes_present,
                    "api_workflows_exported": api_workflows_present,
                    "comfyui_running": len(instances) > 0,
                    "live_instances": len(instances),
                    "manifest": manifest_info,
                })

            # Workflow detail : /api/workflow/<template_id>
            if self.path.startswith("/api/workflow/"):
                tid = urllib.parse.unquote(self.path[len("/api/workflow/"):])
                return self._json(get_workflow_details(tid))

            # Commands list & status
            if self.path == "/api/commands":
                # Strip cmd lists from output (they expose absolute paths)
                pub = {cid: {k: v for k, v in c.items() if k != "cmd"}
                       for cid, c in COMMAND_WHITELIST.items()}
                with COMMAND_LOCK:
                    return self._json({"available": pub,
                                        "running": dict(COMMAND_PROCESSES)})

            # Tail a command's log : /api/command/log/<cmd_id>
            if self.path.startswith("/api/command/log/"):
                cmd_id = self.path[len("/api/command/log/"):]
                with COMMAND_LOCK:
                    info = COMMAND_PROCESSES.get(cmd_id)
                if not info:
                    return self._json({"error": "unknown command"}, status=404)
                log_path = info.get("log")
                tail = ""
                if log_path and Path(log_path).exists():
                    try:
                        with open(log_path, encoding="utf-8") as f:
                            tail = f.read()[-8000:]
                    except Exception:
                        pass
                return self._json({**info, "tail": tail})

        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        if not self._check_auth():
            return self._json({"error": "auth required"}, status=401)

        if self.path == "/api/command":
            body = self._read_body()
            cid = body.get("id")
            params = body.get("params") or {}
            if cid not in COMMAND_WHITELIST:
                return self._json({"error": "unknown or non-whitelisted command"}, status=403)
            spec = COMMAND_WHITELIST[cid]

            # Build extra args from validated params
            extra = []
            for p in spec["params"]:
                if p["name"] in params:
                    val = params[p["name"]]
                    if p["type"] == "int":
                        try: val = int(val)
                        except Exception:
                            return self._json({"error": f"param {p['name']} must be int"}, status=400)
                    extra.extend([p["flag"], str(val)])
                elif "default" in p:
                    extra.extend([p["flag"], str(p["default"])])

            # Generate a unique ID for this run
            run_id = f"{cid}_{uuid.uuid4().hex[:6]}"
            t = threading.Thread(
                target=run_command_async,
                args=(run_id, spec["cmd"], extra), daemon=True,
            )
            t.start()
            return self._json({"ok": True, "id": run_id, "label": spec["label"]})

        if self.path == "/api/job":
            body = self._read_body()
            tid = body.get("template_id")
            prompt = body.get("prompt") or None
            if not tid:
                return self._json({"error": "template_id required"}, status=400)

            classes = load_classes()
            if tid not in classes:
                return self._json({"error": f"unknown template: {tid}"}, status=404)

            instance = pick_least_loaded(discover_instances_cached())
            if not instance:
                # Auto-launch ComfyUI if no instance is alive
                if DEFAULT_COMFYUI_PATH.exists():
                    print(f"[orchestrator] No live instance — auto-launching ComfyUI on 8188")
                    try:
                        launch_comfyui(port=8188)
                        if wait_alive("http://127.0.0.1:8188", timeout=180):
                            # Invalidate cache and re-discover
                            _instances_cache["ts"] = 0
                            instance = pick_least_loaded(discover_instances_cached())
                    except Exception as exc:
                        print(f"[orchestrator] auto-launch failed: {exc}")
                if not instance:
                    return self._json({
                        "error": "no live ComfyUI instance and auto-launch failed",
                        "hint": "Run command 'bootstrap_install' if not yet installed, then 'launch_comfyui'",
                    }, status=503)

            results = load_results()
            run_id = datetime.now().isoformat()
            with ACTIVE_JOBS_LOCK:
                if tid in ACTIVE_JOBS:
                    return self._json({"error": "job already running for this template",
                                        "tid": tid}, status=409)
                ACTIVE_JOBS[tid] = {"status": "submitting",
                                      "instance": instance["url"],
                                      "started_at": time.time()}

            # Spawn in background
            t = threading.Thread(
                target=submit_job_async,
                args=(tid, classes[tid], prompt, instance, results, run_id),
                daemon=True,
            )
            t.start()
            return self._json({"ok": True, "template_id": tid, "instance": instance["url"]})

        self.send_response(404)
        self.end_headers()


def cmd_serve(args):
    token = "" if args.no_auth else get_or_create_token()
    DashboardHandler.expected_token = token
    server = http.server.ThreadingHTTPServer(("0.0.0.0", args.port), DashboardHandler)
    print(f"🎛  Dashboard on http://0.0.0.0:{args.port}/dashboard")
    if token:
        print(f"   🔑 Bearer token (collez-le dans l'UI): {token}")
        print(f"      stored in: {TOKEN_FILE}")
    else:
        print(f"   ⚠  Auth disabled (--no-auth)")
    print(f"   API endpoints:")
    print(f"      GET  /api/dashboard")
    print(f"      GET  /api/instances")
    print(f"      GET  /api/jobs")
    print(f"      POST /api/job  {{ template_id, prompt? }}")
    server.serve_forever()


# ── CLI ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = parser.add_subparsers(dest="cmd", required=True)

    p_status = sp.add_parser("status", help="Liste les instances vivantes + résultats")
    p_status.set_defaults(func=cmd_status)

    p_launch = sp.add_parser("launch", help="Lance une instance ComfyUI")
    p_launch.add_argument("--install-path", type=Path, default=DEFAULT_COMFYUI_PATH)
    p_launch.add_argument("--port", type=int, default=8188)
    p_launch.add_argument("--wait", action="store_true")
    p_launch.set_defaults(func=cmd_launch)

    p_test = sp.add_parser("test", help="Lance les tests sur les instances dispo")
    p_test.add_argument("--phase", type=int, default=1)
    p_test.add_argument("--all", action="store_true")
    p_test.add_argument("--limit", type=int, default=0)
    p_test.add_argument("--timeout", type=int, default=600)
    p_test.add_argument("--skip-done", action="store_true")
    p_test.add_argument("--auto-launch", action="store_true",
                         help="Lance ComfyUI si aucune instance n'est dispo")
    p_test.set_defaults(func=cmd_test)

    p_serve = sp.add_parser("serve", help="Sert le dashboard HTML + API JSON")
    p_serve.add_argument("--port", type=int, default=9000)
    p_serve.add_argument("--no-auth", action="store_true",
                          help="Désactive l'auth Bearer (DEV uniquement)")
    p_serve.set_defaults(func=cmd_serve)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
