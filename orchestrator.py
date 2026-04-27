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
    # Use the venv Python (defined in SUBPROCESS_PYTHON) so torch+CUDA are
    # picked up. SUBPROCESS_PYTHON resolves at call time, after it's been set.
    # `-u` keeps stdout unbuffered so live logs reach the captured file.
    cmd = [
        SUBPROCESS_PYTHON, "-u", str(install_path / "main.py"),
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
        # CREATE_NO_WINDOW (0x08000000) — no flickering console. Combined with
        # CREATE_NEW_PROCESS_GROUP (0x00000200) so Ctrl+C in the parent doesn't
        # propagate to ComfyUI; the orchestrator can still kill it via PID.
        # NB: do not use DETACHED_PROCESS here — it conflicts with
        # CREATE_NO_WINDOW and can spawn a console on some Windows versions.
        proc = subprocess.Popen(
            cmd, cwd=str(install_path),
            stdout=log_handle, stderr=subprocess.STDOUT,
            creationflags=0x08000000 | subprocess.CREATE_NEW_PROCESS_GROUP,
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
    log_event("info", "job", f"submitted: {template_id} → {instance['url']}",
              template_id=template_id, instance=instance["url"])
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
        log_event("error", "job", f"failed: {template_id} — API not exported",
                  template_id=template_id)
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
        log_event("error", "job", f"crashed: {template_id} — {exc}",
                  template_id=template_id)
    else:
        # Reached only when no exception; log success/error/timeout
        rs = results["by_workflow"][template_id].get("status", "unknown")
        dur = results["by_workflow"][template_id].get("duration", 0)
        out_count = len(results["by_workflow"][template_id].get("outputs", []))
        kind = "ok" if rs == "ok" else ("warn" if rs == "timeout" else "error")
        log_event(kind, "job",
                  f"{rs}: {template_id} ({dur}s, {out_count} output{'s' if out_count != 1 else ''})",
                  template_id=template_id, duration=dur)
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


def _pick_subprocess_python() -> str:
    """Pick the Python interpreter to use for whitelisted sub-commands.

    The orchestrator may be launched from anywhere — system Python, a venv,
    a packaged install — but the sub-commands (`comfyui_catalog.py`, etc.)
    need access to packages like `comfyui_workflow_templates_core` that are
    typically installed in the project's venv only. Auto-detect that venv:

      <repo>/venv/Scripts/python.exe         (sibling, Windows)
      <repo>/venv/bin/python                  (sibling, Unix)
      <repo>/../venv/Scripts/python.exe       (one level up — the user's
      <repo>/../venv/bin/python                creation-ops layout)

    Fall back to sys.executable if no usable venv is found.
    """
    is_win = os.name == "nt"
    candidates = []
    for parent in (SCRIPTS_DIR, SCRIPTS_DIR.parent, SCRIPTS_DIR.parent.parent):
        if is_win:
            candidates.append(parent / "venv" / "Scripts" / "python.exe")
            candidates.append(parent / ".venv" / "Scripts" / "python.exe")
        else:
            candidates.append(parent / "venv" / "bin" / "python")
            candidates.append(parent / ".venv" / "bin" / "python")

    for c in candidates:
        if not c.exists():
            continue
        # Verify the package the sub-commands need is importable in that venv
        try:
            r = subprocess.run(
                [str(c), "-c", "import comfyui_workflow_templates_core"],
                capture_output=True, timeout=5,
            )
            if r.returncode == 0:
                return str(c)
        except Exception:
            continue
    return sys.executable


SUBPROCESS_PYTHON = _pick_subprocess_python()
if SUBPROCESS_PYTHON != sys.executable:
    print(f"[orchestrator] Sub-commands will use: {SUBPROCESS_PYTHON}")
else:
    print(f"[orchestrator] Sub-commands will use sys.executable: {sys.executable}")
COMMAND_WHITELIST = {
    "catalog_build": {
        "label": "📦 Build catalog",
        "description": "Construit le manifest (latest + biggest variant)",
        "cmd": [SUBPROCESS_PYTHON, "-u", str(SCRIPTS_DIR / "comfyui_catalog.py"), "build"],
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
        "cmd": [SUBPROCESS_PYTHON, "-u", str(SCRIPTS_DIR / "comfyui_catalog.py"), "sync"],
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
        "cmd": [SUBPROCESS_PYTHON, "-u", str(SCRIPTS_DIR / "comfyui_catalog.py"), "cleanup", "--yes"],
        "params": [],
    },
    "catalog_download": {
        "label": "📥 Download models",
        "description": "Télécharge les modèles du manifest via l'extension",
        "cmd": [SUBPROCESS_PYTHON, "-u", str(SCRIPTS_DIR / "comfyui_catalog.py"), "download"],
        "params": [],
    },
    "catalog_status": {
        "label": "📊 Catalog report",
        "description": "Affiche le rapport du manifest courant",
        "cmd": [SUBPROCESS_PYTHON, "-u", str(SCRIPTS_DIR / "comfyui_catalog.py"), "report"],
        "params": [],
    },
    "classify": {
        "label": "🏷️ Classify workflows",
        "description": "Classifie les 218 workflows en 10 phases × 19 catégories",
        "cmd": [SUBPROCESS_PYTHON, "-u", str(SCRIPTS_DIR / "classify_workflows.py")],
        "params": [],
    },
    "export_api": {
        "label": "🔄 Export workflows → API",
        "description": "Convertit les workflows UI en format API (pour /prompt)",
        "cmd": [SUBPROCESS_PYTHON, "-u", str(SCRIPTS_DIR / "export_workflows_api.py")],
        "params": [],
    },
    "starters": {
        "label": "🌱 Generate starter images",
        "description": "Génère 14 images contextuelles par catégorie",
        "cmd": [SUBPROCESS_PYTHON, "-u", str(SCRIPTS_DIR / "generate_starters.py")],
        "params": [],
    },
    "gated_check": {
        "label": "🔐 Check gated HF models",
        "description": "Identifie les modèles HuggingFace nécessitant licence",
        "cmd": [SUBPROCESS_PYTHON, "-u", str(SCRIPTS_DIR / "check_gated_models.py")],
        "params": [],
    },
    "install_workflows": {
        "label": "📁 Install workflows in ComfyUI",
        "description": "Copie les 218 workflows dans user/default/workflows/",
        "cmd": [SUBPROCESS_PYTHON, "-u", str(SCRIPTS_DIR / "comfyui_catalog.py"), "install-workflows"],
        "params": [],
    },
    "launch_comfyui": {
        "label": "🚀 Launch ComfyUI instance",
        "description": "Démarre une nouvelle instance sur le port donné",
        "cmd": [SUBPROCESS_PYTHON, "-u", str(SCRIPTS_DIR / "orchestrator.py"), "launch"],
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
        "cmd": [SUBPROCESS_PYTHON, "-u", str(SCRIPTS_DIR / "comfyui_catalog.py"), "build",
                "--budget", "250", "--max-age-years", "2"],
        "params": [],
    },
    "bundle_image": {
        "label": "🖼 Bundle: Image-focused (400 GB)",
        "description": "Image generation + edit complet (Flux, Qwen-Edit, HiDream, ControlNet) sans video.",
        "cmd": [SUBPROCESS_PYTHON, "-u", str(SCRIPTS_DIR / "comfyui_catalog.py"), "build",
                "--budget", "400", "--max-age-years", "2"],
        "params": [],
    },
    "bundle_standard": {
        "label": "🎬 Bundle: Standard (700 GB)",
        "description": "Recommandé : image + video latest (Wan2.2, LTX 2.3, Hunyuan Video 1.5).",
        "cmd": [SUBPROCESS_PYTHON, "-u", str(SCRIPTS_DIR / "comfyui_catalog.py"), "build",
                "--budget", "700", "--max-age-years", "2"],
        "params": [],
    },
    "bundle_full": {
        "label": "🎯 Bundle: Full catalog (1500 GB)",
        "description": "Toutes les latest versions sans contrainte budget — pour station haut de gamme.",
        "cmd": [SUBPROCESS_PYTHON, "-u", str(SCRIPTS_DIR / "comfyui_catalog.py"), "build",
                "--budget", "1500", "--max-age-years", "2"],
        "params": [],
    },

    # ── Macro: apply changes ─────────────────────────────────────────────
    "apply_changes": {
        "label": "✨ Apply changes (build + download + cleanup)",
        "description": "Macro complète : rebuild manifest avec nouveau budget → télécharge les nouveaux modèles → supprime les obsolètes. À utiliser après un changement de bundle ou de budget.",
        "cmd": [SUBPROCESS_PYTHON, "-u", str(SCRIPTS_DIR / "comfyui_catalog.py"), "apply"],
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

# ── Live event log (ring buffer; consumed by the dashboard's live stream) ───
import collections
EVENT_LOG: collections.deque = collections.deque(maxlen=500)
EVENT_LOG_LOCK = threading.Lock()
_EVENT_NEXT_ID = 1

def log_event(kind: str, source: str, message: str, **extra):
    """Append a structured event to the live log.

    kind     — 'info' | 'ok' | 'warn' | 'error'
    source   — short tag, e.g. 'command', 'job', 'instance', 'system'
    message  — human-readable line
    extra    — arbitrary JSON-serializable fields (id, duration, …)
    """
    global _EVENT_NEXT_ID
    with EVENT_LOG_LOCK:
        ev = {
            "id": _EVENT_NEXT_ID,
            "ts": time.time(),
            "kind": kind, "source": source, "message": message,
            **extra,
        }
        _EVENT_NEXT_ID += 1
        EVENT_LOG.append(ev)


def get_events_since(since_id: int = 0, limit: int = 200) -> list[dict]:
    with EVENT_LOG_LOCK:
        return [e for e in EVENT_LOG if e["id"] > since_id][-limit:]


# ── Model ↔ Workflow graph ──────────────────────────────────────────────
# CURATED family map — collapses fine-grained manifest families (flux_dev,
# flux2_klein, flux_redux, …) into a small set of meaningful canonical
# families. Lets the user see "flux" as one cluster instead of 7 fragments.
# Manifest family → canonical family.
CURATED_FAMILY_MAP = {
    # Flux variants — all collapse to one
    "flux_dev":          "flux",
    "flux2_main":        "flux",
    "flux2_klein":       "flux",
    "flux_controlnet":   "flux",
    "flux_fill":         "flux",
    "flux_redux":        "flux",
    "flux_uso":          "flux",

    # Qwen variants
    "qwen_image":        "qwen",
    "qwen_image_edit":   "qwen",
    "qwen_text_encoder": "qwen",

    # Hunyuan variants
    "hunyuan_video":     "hunyuan",
    "hunyuan3d":         "hunyuan",

    # SD variants
    "sd3_5":             "sd3",

    # Audio
    "audio_ace":         "audio",

    # Aux / shared utilities
    "aux_text_encoder":  "aux",
    "aux_upscaler":      "aux",
}

# Main vs aux: when a workflow uses several families, the *primary* family
# (used for colour-coding the workflow node) prefers main families over aux.
MAIN_FAMILIES = {
    "flux", "qwen", "sdxl", "sd3", "sd1.5",
    "wan_video", "hunyuan", "ltx_video", "z_image", "kandinsky",
    "chroma", "lumina", "anima", "omnigen", "ovis", "lotus",
    "ernie", "hidream_i1", "audio",
}

# Family palette — amber-friendly with distinct hues for each canonical
# family. Picked to read against the CRT amber-on-black background.
FAMILY_COLORS = {
    "flux":         "#88b6ff",  # blue
    "qwen":         "#f59e0b",  # warm amber
    "sdxl":         "#7ad67a",  # green
    "sd3":          "#a3e635",  # lime
    "sd1.5":        "#bef264",  # pale lime
    "wan_video":    "#c084fc",  # violet
    "z_image":      "#ff9166",  # coral
    "kandinsky":    "#ff79c6",  # pink
    "ltx_video":    "#22d3ee",  # cyan
    "hunyuan":      "#fbbf24",  # gold
    "chroma":       "#fcd34d",  # straw
    "lumina":       "#a78bfa",  # mauve
    "anima":        "#f472b6",  # rose
    "omnigen":      "#34d399",  # mint
    "ovis":         "#67e8f9",  # sky
    "lotus":        "#fb923c",  # orange
    "ernie":        "#facc15",  # yellow
    "hidream_i1":   "#fda4af",  # blush
    "audio":        "#94a3b8",  # slate
    "aux":          "#9ca3af",  # gray (shared utility)
}
DEFAULT_FAMILY_COLOR = "#b8a57a"  # text-dim amber

def curated_family(raw: str) -> str:
    """Map a raw manifest family to a curated canonical family."""
    raw = (raw or "unknown").lower()
    return CURATED_FAMILY_MAP.get(raw, raw)


# ── Version / upgrade detection ─────────────────────────────────────────
# Heuristic: model filenames often look like "<core>-<date>-<size>-<quant>.safetensors".
# We extract the "core" (the bit before any version/size/quant marker), the date
# (4-digit YYMM marker like 2509 / 2511), and the size class (9b / 14b) so that we
# can group same-product variants and propose temporal upgrades within each group.
import re as _re

_VERSION_MARKER_RE = _re.compile(
    r"(?<![0-9])\d{4}(?![0-9])"          # date marker: 2509, 2511, 2602
    r"|\d+b(?![0-9a-z])"                  # size class: 9b, 14b, 4b
    r"|fp\d+|bf\d+|int\d+"                # quantization
    r"|\d+\s*steps?"                      # 4steps, 8steps
    r"|\bv\d+(?:\.\d+)*"                  # v1, v1.0
    r"|\b(?:lightning|turbo|distill(?:ed)?|scaled|mixed|fixed|lite|small|medium|hd|md|sm)",
    flags=_re.IGNORECASE,
)
_DATE_RE = _re.compile(r"(?<![0-9])(\d{4})(?![0-9])")
_SIZE_RE = _re.compile(r"(\d+b)(?![0-9a-z])", flags=_re.IGNORECASE)


def extract_core_name(filename: str) -> str:
    """Strip the version/quantization/size suffix from a filename to get the
    canonical 'product name'. Used to group variants:
        qwen_image_edit_2509_fp8_e4m3fn  →  qwen-image-edit
        Qwen_Image_Edit_2511-SYSTMS_INFL8 →  qwen-image-edit
        flux-2-klein-9b-fp8              →  flux-2-klein
        flux-2-klein-base-4b             →  flux-2-klein-base
    """
    name = filename.lower().rsplit(".", 1)[0]
    name = _re.sub(r"[-_]+", "-", name)
    m = _VERSION_MARKER_RE.search(name)
    if m:
        name = name[: m.start()]
    return name.rstrip("-")


def extract_release_date(filename: str) -> int:
    """Highest 4-digit YYMM-like marker (2509 < 2511 < 2602). 0 if none."""
    matches = _DATE_RE.findall(filename.lower())
    return max((int(m) for m in matches), default=0)


def extract_size_class(filename: str) -> str:
    """e.g. '9b', '14b'. Empty string if no size marker."""
    m = _SIZE_RE.search(filename.lower())
    return m.group(1) if m else ""


_FLAVOR_TOKEN_RE = _re.compile(
    r"\b(lightning|turbo|distill(?:ed)?|lite|small|medium|hd|md|sm"
    r"|fp\d+|bf\d+|int\d+|e\d+m\d+|scaled|mixed|fixed"
    r"|\d+steps?|lora|loras|controlnet|union|dev|schnell|base)\b",
    flags=_re.IGNORECASE,
)


def extract_flavor_tokens(filename: str) -> set:
    """Extract distinctive tokens from a filename for flavor matching:
    Lightning, fp8, 4steps, lora, etc. Used to pick the best upgrade
    candidate (Lightning 2509 → Lightning 2511, not Lightning 2509 → SYSTMS 2511).
    """
    return {m.lower() for m in _FLAVOR_TOKEN_RE.findall(filename.lower())}


def compute_upgrades() -> dict:
    """Detect workflows that load an older variant of a model whose newer
    variant exists in the catalog.

    Reads `all_models_cache.json` (the full ComfyUI catalog — every model
    referenced by any of the 218 reference workflows) — NOT just the
    `manifest.json` selection-by-budget. This way we can catch upgrades
    even when the older 2509 variant is no longer in the budget but is
    still wired into a reference workflow.

    Strategy:
      1. Group models by (core, size_class, family). Same core + same size +
         same family ⇒ same product line.
      2. Within each group, sort by extract_release_date. The largest date
         is the recommended version; older ones are upgradeable to it.
      3. For every workflow that uses an older variant, surface an upgrade
         hint: { from: old_filename, to: new_filename, reason }.

    Returns a structure consumable by the dashboard:
      {
        "by_workflow": {tid: [{from, to, from_date, to_date, family, size}, ...]},
        "by_model":    {old_filename: new_filename},
        "groups":      [{core, size, family, variants: [{name, date, used_in}]}, ...],
        "stats":       {workflows_with_upgrades, total_upgrade_pairs, groups},
      }
    """
    # Prefer the full all-models cache; fall back to the budgeted manifest.
    cache_path = STATE_DIR / "all_models_cache.json"
    manifest_path = STATE_DIR / "manifest.json"

    selected = None
    source_used = None
    if cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
            selected = cache.get("models") or []
            source_used = "all_models_cache"
        except Exception:
            selected = None

    if selected is None and manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            selected = manifest.get("selected_models") or []
            source_used = "manifest"
        except Exception:
            selected = None

    if not selected:
        return {"error": "neither all_models_cache nor manifest available — run catalog_build",
                "by_workflow": {}, "by_model": {}, "groups": [], "stats": {}}

    # Group by (curated_family, core, size)
    groups: dict[tuple, list] = {}
    for m in selected:
        fname = m.get("name") or "?"
        family = curated_family(m.get("family") or "unknown")
        core = extract_core_name(fname)
        size = extract_size_class(fname)
        key = (family, core, size)
        groups.setdefault(key, []).append({
            "name": fname,
            "date": extract_release_date(fname),
            "size": size,
            "family": family,
            "core": core,
            "used_in": list(m.get("used_in") or []),
            "directory": m.get("directory") or "",
            "size_gb": round((m.get("size") or 0) / 1024**3, 2),
        })

    by_workflow: dict[str, list] = {}
    by_model: dict[str, str] = {}
    group_summaries = []

    for (family, core, size), variants in groups.items():
        dates = [v["date"] for v in variants]
        if len(variants) < 2 or max(dates) == 0:
            continue
        variants.sort(key=lambda v: v["date"], reverse=True)
        latest_date = variants[0]["date"]
        # Only consider as "older" variants whose date is STRICTLY less.
        # Same-date alternative builds (e.g. two 2511 builds — one Lightning,
        # one SYSTMS) are siblings, not upgrades.
        older = [v for v in variants if v["date"] < latest_date and v["date"] > 0]
        if not older:
            continue
        # All candidates at the latest date — for each older variant we'll
        # pick the latest-date candidate that best matches its flavor tokens
        # (Lightning ↔ Lightning, not Lightning ↔ SYSTMS).
        latest_candidates = [v for v in variants if v["date"] == latest_date]

        # Default "group latest" (for the summary): the most-used candidate
        group_latest = max(latest_candidates, key=lambda v: len(v["used_in"]))

        group_summaries.append({
            "family": family, "core": core, "size": size,
            "variants": [
                {"name": v["name"], "date": v["date"], "used_in_count": len(v["used_in"])}
                for v in variants
            ],
            "latest": group_latest["name"],
            "latest_date": latest_date,
        })

        for v in older:
            old_flavor = extract_flavor_tokens(v["name"])
            # Pick the latest candidate whose flavor tokens overlap most with
            # the old's. Tiebreak: most-used.
            def score(cand):
                cand_flavor = extract_flavor_tokens(cand["name"])
                overlap = len(old_flavor & cand_flavor)
                # Penalise candidates that introduce flavors the old didn't have
                introduced = len(cand_flavor - old_flavor)
                return (overlap, -introduced, len(cand["used_in"]))
            best = max(latest_candidates, key=score)
            by_model[v["name"]] = best["name"]
            shared = old_flavor & extract_flavor_tokens(best["name"])
            flavor_note = f" · same flavor [{', '.join(sorted(shared))}]" if shared else ""
            for tid in v["used_in"]:
                by_workflow.setdefault(tid, []).append({
                    "from": v["name"],
                    "to": best["name"],
                    "from_date": v["date"],
                    "to_date": latest_date,
                    "family": family,
                    "size": size,
                    "reason": f"newer release ({v['date']} -> {latest_date}){flavor_note}",
                })

    # Sort each workflow's upgrades by date jump (biggest first)
    for tid in by_workflow:
        by_workflow[tid].sort(key=lambda u: u["to_date"] - u["from_date"], reverse=True)

    return {
        "by_workflow": by_workflow,
        "by_model": by_model,
        "groups": sorted(group_summaries, key=lambda g: (g["family"], g["core"])),
        "stats": {
            "workflows_with_upgrades": len(by_workflow),
            "total_upgrade_pairs": sum(len(v) for v in by_workflow.values()),
            "groups": len(group_summaries),
            "source": source_used,
            "models_scanned": len(selected),
        },
    }


def family_color(family: str) -> str:
    """Pick a colour for an unknown family by hashing it into the palette."""
    if not family: return DEFAULT_FAMILY_COLOR
    if family in FAMILY_COLORS: return FAMILY_COLORS[family]
    # Stable hash → pick from a fallback palette
    fallback = ["#67e8f9", "#fda4af", "#fde047", "#86efac", "#fcd34d",
                "#a5f3fc", "#fdba74", "#d8b4fe", "#bef264", "#67e8f9"]
    h = sum(ord(c) for c in family)
    return fallback[h % len(fallback)]


def compute_model_graph() -> dict:
    """Build a workflow ↔ model bipartite graph from the catalog manifest.

    Returns:
      {
        "nodes": [{id, type, label, family, color, ...}],
        "edges": [{source, target}],
        "families": {family_name: hex_color},
        "stats":   {workflows, models, edges, families}
      }
    """
    manifest_path = STATE_DIR / "manifest.json"
    if not manifest_path.exists():
        return {"error": "manifest not built yet — run catalog_build", "nodes": [], "edges": [], "families": {}, "stats": {}}

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"error": f"manifest read failed: {exc}", "nodes": [], "edges": [], "families": {}, "stats": {}}

    selected = manifest.get("selected_models") or []
    nodes_workflow: dict[str, dict] = {}
    nodes_model: dict[str, dict] = {}
    edges: list[dict] = []
    families_seen: set[str] = set()

    for m in selected:
        fname = m.get("name") or "?"
        # Use the curated canonical family — collapses flux_dev/flux2_klein/etc.
        # into a single "flux" cluster, etc.
        raw_family = (m.get("family") or "unknown").lower()
        family = curated_family(raw_family)
        families_seen.add(family)
        size_gb = round((m.get("size") or 0) / 1024**3, 2)
        mid = f"model:{fname}"
        nodes_model[mid] = {
            "id": mid,
            "type": "model",
            "label": fname,
            "family": family,
            "raw_family": raw_family,           # keep the fine-grained tag
            "color": family_color(family),
            "size_gb": size_gb,
            "directory": m.get("directory") or "",
            "version_label": m.get("version_label") or "",
            "url": m.get("url") or "",
        }
        for tid in (m.get("used_in") or []):
            wid = f"wf:{tid}"
            if wid not in nodes_workflow:
                nodes_workflow[wid] = {
                    "id": wid,
                    "type": "workflow",
                    "label": tid,
                    "family": family,
                    "color": family_color(family),
                    "model_count": 0,
                }
            nodes_workflow[wid]["model_count"] += 1
            edges.append({"source": wid, "target": mid})

    # Re-assign each workflow's primary family. Strategy:
    #   1. Tally families used by the workflow's models
    #   2. Prefer MAIN families over aux/utility ones
    #   3. Within the same tier, pick the most-used family
    workflow_family_tally: dict[str, dict[str, int]] = {}
    for e in edges:
        wf = e["source"]; mid = e["target"]
        fam = nodes_model[mid]["family"]
        workflow_family_tally.setdefault(wf, {}).setdefault(fam, 0)
        workflow_family_tally[wf][fam] += 1
    for wid, tally in workflow_family_tally.items():
        # Split into main vs other; pick from main if any
        main_tally = {f: c for f, c in tally.items() if f in MAIN_FAMILIES}
        chosen = main_tally if main_tally else tally
        primary = max(chosen.items(), key=lambda kv: kv[1])[0]
        nodes_workflow[wid]["family"] = primary
        nodes_workflow[wid]["color"] = family_color(primary)

    nodes = list(nodes_workflow.values()) + list(nodes_model.values())
    families = {f: family_color(f) for f in sorted(families_seen)}

    return {
        "nodes": nodes,
        "edges": edges,
        "families": families,
        "stats": {
            "workflows": len(nodes_workflow),
            "models": len(nodes_model),
            "edges": len(edges),
            "families": len(families),
        },
    }


# ── API documentation registry ──────────────────────────────────────────
# Single source of truth for what the dashboard exposes. Drives /docs.
API_DOCS = [
    {"section": "Discovery / live state"},
    {"method": "GET", "path": "/api/dashboard",
     "desc": "Aggregate snapshot: stats, instances, workflow groups. Used by the dashboard at refresh time.",
     "auth": "localhost-bypass"},
    {"method": "GET", "path": "/api/instances",
     "desc": "List of ComfyUI instances reachable from the orchestrator with their queue load.",
     "auth": "localhost-bypass"},
    {"method": "GET", "path": "/api/jobs",
     "desc": "Active jobs currently being processed by ComfyUI instances.",
     "auth": "localhost-bypass"},
    {"method": "GET", "path": "/api/setup",
     "desc": "First-run check: which prerequisites are missing (comfyui installed, manifest built, classifications, etc.).",
     "auth": "localhost-bypass"},
    {"method": "GET", "path": "/api/events?since=<id>",
     "desc": "Live event stream — long-poll style. Returns events with id > since (max 200). Empty list when nothing new.",
     "auth": "localhost-bypass",
     "response": '{"events": [{"id": 42, "ts": 1714286400.0, "kind": "ok", "source": "command", "message": "ok: catalog_status_xxx (rc=0, 7.2s)"}], "last_id": 42, "buffer_size": 47}'},

    {"section": "Workflows"},
    {"method": "GET", "path": "/api/workflow/<template_id>",
     "desc": "Full details on a workflow: classification, default prompts, key widgets, last run, API JSON.",
     "auth": "localhost-bypass"},
    {"method": "POST", "path": "/api/job",
     "desc": "Submit a workflow as a job. The orchestrator picks the least-loaded ComfyUI and forwards to its /prompt endpoint.",
     "auth": "localhost-bypass",
     "body": '{"template_id": "flux_schnell", "prompt": "a parrot on a bicycle"}'},
    {"method": "GET", "path": "/api/preview?budget=<gb>",
     "desc": "Dry-run a budget change: shows which models would be added / removed / kept under that budget.",
     "auth": "localhost-bypass"},
    {"method": "GET", "path": "/api/model-graph",
     "desc": "Bipartite workflow ↔ model graph for visualization. Nodes (workflows + models), edges (uses-this-model), per-family colours.",
     "auth": "localhost-bypass",
     "response": '{"nodes": [{"id":"wf:flux_schnell","type":"workflow","family":"flux","color":"#88b6ff","model_count":3}, {"id":"model:flux1-schnell.safetensors","type":"model","family":"flux","color":"#88b6ff","size_gb":11.9}], "edges":[{"source":"wf:flux_schnell","target":"model:flux1-schnell.safetensors"}], "families":{"flux":"#88b6ff","sdxl":"#7ad67a"}, "stats":{"workflows":204,"models":136,"edges":612,"families":13}}'},
    {"method": "GET", "path": "/api/upgrades",
     "desc": ("Detects workflows loading an older variant of a model whose newer variant is in the catalog. "
              "Same core name (e.g. qwen-image-edit) + same size class + newer date marker (2509 → 2511). "
              "Returns per-workflow recommendations to swap old → new."),
     "auth": "localhost-bypass",
     "response": '{"by_workflow":{"image_qwen_edit_2509":[{"from":"qwen_image_edit_2509_fp8_e4m3fn.safetensors","to":"Qwen_Image_Edit_2511-SYSTMS_INFL8.safetensors","from_date":2509,"to_date":2511,"family":"qwen","reason":"newer release (2509 → 2511)"}]},"by_model":{"qwen_image_edit_2509_fp8_e4m3fn.safetensors":"Qwen_Image_Edit_2511-SYSTMS_INFL8.safetensors"},"stats":{"workflows_with_upgrades":7,"total_upgrade_pairs":12,"groups":4}}'},

    {"section": "Commands"},
    {"method": "GET", "path": "/api/commands",
     "desc": "List of whitelisted commands and the currently-running ones.",
     "auth": "localhost-bypass"},
    {"method": "POST", "path": "/api/command",
     "desc": "Run a whitelisted command. Returns {ok, id, label}. Logs are captured.",
     "auth": "localhost-bypass",
     "body": '{"id": "catalog_build", "params": {"budget": 700, "max_age_years": 2}}'},
    {"method": "GET", "path": "/api/command/log/<run_id>",
     "desc": "Run info + last 8 KB of the command's stdout/stderr.",
     "auth": "localhost-bypass"},
    {"method": "POST", "path": "/api/command/cancel/<run_id>",
     "desc": "Kill a running command (taskkill on Windows, SIGTERM/SIGKILL elsewhere). Marks the run as 'cancelled'.",
     "auth": "localhost-bypass"},

    {"section": "Authentication"},
    {"method": "—", "path": "Bearer token",
     "desc": ("Each request can pass `Authorization: Bearer <token>`. The token is generated on first run and "
              "auto-injected into the dashboard HTML via `window.ORCHESTRATOR_TOKEN`. Localhost (127.0.0.1, ::1) "
              "is exempt by default; set `ORCHESTRATOR_REQUIRE_AUTH=1` in the env to enforce auth even for localhost. "
              "Token file: `<state_dir>/auth_token`."),
     "auth": "—"},
]


def run_command_async(cmd_id: str, cmd: list[str], extra_args: list[str]):
    """Run a command in a thread, capture stdout, persist log."""
    full_cmd = cmd + extra_args
    log_file = STATE_DIR / f"cmd_{cmd_id}_{int(time.time())}.log"
    log_handle = open(log_file, "w", encoding="utf-8")
    log_event("info", "command", f"started: {cmd_id}", run_id=cmd_id, cmd=" ".join(full_cmd))
    try:
        # CRITICAL on Windows: CREATE_NO_WINDOW (0x08000000) prevents Python
        # console apps from popping a flickering window. Without it, every
        # spawned python.exe gets its own console, and if the user accidentally
        # closes that window the subprocess dies with STATUS_CONTROL_C_EXIT
        # (rc=3221225786) — exactly what the user was seeing. Stdout is still
        # captured to log_handle, so progress shows up in the dashboard's
        # live event stream regardless.
        popen_kwargs = dict(
            stdout=log_handle, stderr=subprocess.STDOUT,
            cwd=str(Path(__file__).parent.parent),
            text=True, encoding="utf-8",
        )
        if os.name == "nt":
            popen_kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
        proc = subprocess.Popen(full_cmd, **popen_kwargs)
        with COMMAND_LOCK:
            COMMAND_PROCESSES[cmd_id] = {
                "pid": proc.pid, "log": str(log_file),
                "started_at": time.time(),
                "cmd": " ".join(full_cmd),
                "status": "running",
            }
        rc = proc.wait()
        with COMMAND_LOCK:
            # If the run was cancelled, preserve the cancelled status
            if COMMAND_PROCESSES[cmd_id].get("status") != "cancelled":
                COMMAND_PROCESSES[cmd_id]["status"] = "ok" if rc == 0 else "error"
            COMMAND_PROCESSES[cmd_id]["return_code"] = rc
            COMMAND_PROCESSES[cmd_id]["finished_at"] = time.time()
            final_status = COMMAND_PROCESSES[cmd_id]["status"]
        duration = round(time.time() - COMMAND_PROCESSES[cmd_id]["started_at"], 1)
        log_event(
            "ok" if final_status == "ok" else ("warn" if final_status == "cancelled" else "error"),
            "command", f"{final_status}: {cmd_id} (rc={rc}, {duration}s)",
            run_id=cmd_id, return_code=rc, duration=duration,
        )
    except Exception as exc:
        with COMMAND_LOCK:
            COMMAND_PROCESSES[cmd_id] = {
                "status": "error", "error": str(exc),
                "started_at": time.time(),
            }
        log_event("error", "command", f"crashed: {cmd_id} — {exc}", run_id=cmd_id)
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


def _render_docs_html() -> str:
    """Render the API_DOCS spec as a self-contained HTML page in magascii style."""
    rows_html = []
    for entry in API_DOCS:
        if "section" in entry:
            rows_html.append(f'<h2 class="section">{html_escape(entry["section"])}</h2>')
            continue
        method = entry.get("method", "—")
        path = entry.get("path", "—")
        desc = entry.get("desc", "")
        auth = entry.get("auth", "—")
        body = entry.get("body", "")
        resp = entry.get("response", "")
        method_class = method.lower().replace("—", "info").replace("/", "-")
        rows_html.append(f"""
        <div class="endpoint">
          <div class="ep-head">
            <span class="m m-{method_class}">{html_escape(method)}</span>
            <code class="path">{html_escape(path)}</code>
            <span class="auth">auth: {html_escape(auth)}</span>
          </div>
          <p class="desc">{html_escape(desc)}</p>
          {f'<div class="ex"><span class="lbl">body</span><pre>{html_escape(body)}</pre></div>' if body else ""}
          {f'<div class="ex"><span class="lbl">response</span><pre>{html_escape(resp)}</pre></div>' if resp else ""}
        </div>""")

    palette_html = "".join(
        f'<span class="fam-pill" style="background:{c}1a;border-color:{c};color:{c}">{html_escape(f)}</span>'
        for f, c in FAMILY_COLORS.items()
    )

    body = "\n".join(rows_html)

    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>orchestrator@magascii — api docs</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Fira+Code:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root {{
    --bg:#0a0806; --screen:#0e0b07; --amber:#f5a30b; --amber-dim:#9c6405;
    --line:rgba(245,163,11,0.18); --line-soft:rgba(245,163,11,0.08);
    --text:#e8d9b0; --text-dim:#b8a57a; --muted:#7a6a4a;
    --ok:#7ad67a; --warn:#ffb648; --err:#ff5555; --blue:#88b6ff;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  html, body {{
    background:#000; color:var(--text);
    font-family:'Fira Code', ui-monospace, monospace;
    font-size: 13px; line-height: 1.5;
  }}
  body::before {{
    content:""; position:fixed; inset:0;
    background: radial-gradient(ellipse at center, var(--bg) 0%, #000 90%);
    pointer-events:none; z-index:0;
  }}
  body::after {{
    content:""; position:fixed; inset:0; pointer-events:none; z-index:9999;
    background: repeating-linear-gradient(0deg,
      rgba(0,0,0,0) 0px, rgba(0,0,0,0) 2px,
      rgba(0,0,0,0.10) 3px, rgba(0,0,0,0.10) 4px);
    mix-blend-mode: multiply;
  }}
  .wrap {{
    position:relative; z-index:1;
    max-width: 980px; margin: 0 auto;
    padding: 20px 24px 80px;
    min-height: 100vh;
    background: var(--screen);
    border-left: 1px solid var(--amber-dim);
    border-right: 1px solid var(--amber-dim);
  }}
  header {{
    display:flex; align-items:baseline; gap:12px;
    padding-bottom: 12px;
    border-bottom: 1px dashed var(--line);
    margin-bottom: 16px;
  }}
  header h1 {{
    color: var(--amber); font-size: 16px;
    letter-spacing: 1px;
  }}
  header .crumb {{ color: var(--muted); font-size: 11.5px; }}
  header .home {{ margin-left: auto; }}
  header a {{
    color: var(--blue); text-decoration: none;
    border-bottom: 1px dotted var(--blue);
  }}
  header a:hover {{ color: #c9defe; }}
  .lead {{
    color: var(--text-dim); font-size: 12.5px; line-height: 1.6;
    margin-bottom: 24px;
  }}
  .lead code {{
    color: var(--amber); background: rgba(245,163,11,0.08);
    padding: 1px 5px; border-radius: 2px;
  }}
  h2.section {{
    color: var(--amber);
    font-size: 12px; letter-spacing: 2px;
    text-transform: uppercase;
    margin: 28px 0 8px;
    padding: 6px 0;
    border-top: 1px dashed var(--line);
    border-bottom: 1px dashed var(--line-soft);
  }}
  h2.section:first-of-type {{ margin-top: 0; border-top: none; }}
  .endpoint {{
    border: 1px solid var(--line);
    background: rgba(0,0,0,0.25);
    padding: 10px 14px;
    margin-bottom: 8px;
  }}
  .ep-head {{
    display:flex; align-items: baseline; gap: 10px;
    margin-bottom: 6px; flex-wrap: wrap;
  }}
  .m {{
    font-weight: 600; padding: 2px 8px;
    border: 1px solid var(--line);
    color: var(--text-dim);
    font-size: 11px; letter-spacing: 0.6px;
  }}
  .m-get  {{ color: var(--blue); border-color: rgba(136,182,255,0.4); }}
  .m-post {{ color: var(--ok);   border-color: rgba(122,214,122,0.4); }}
  .m-info {{ color: var(--muted); }}
  code.path {{
    color: var(--amber); font-weight: 600;
    font-size: 13px;
  }}
  .auth {{
    margin-left: auto;
    color: var(--muted); font-size: 11px; letter-spacing: 0.4px;
  }}
  .desc {{
    color: var(--text-dim); font-size: 12px;
    margin-bottom: 6px;
  }}
  .ex {{ margin-top: 6px; }}
  .ex .lbl {{
    color: var(--muted); font-size: 10.5px;
    text-transform: uppercase; letter-spacing: 0.6px;
    display: block; margin-bottom: 2px;
  }}
  .ex pre {{
    background: rgba(0,0,0,0.45);
    border: 1px solid var(--line);
    padding: 6px 10px;
    font-size: 11.5px; line-height: 1.5;
    color: var(--text); white-space: pre-wrap; word-break: break-all;
    overflow-x: auto; max-height: 220px;
  }}
  .palette {{
    display: flex; flex-wrap: wrap; gap: 6px;
    margin: 10px 0 24px;
  }}
  .fam-pill {{
    border: 1px solid; padding: 2px 10px;
    font-size: 11px; letter-spacing: 0.4px;
  }}
  footer {{
    margin-top: 32px;
    padding-top: 12px;
    border-top: 1px dashed var(--line);
    color: var(--muted); font-size: 11px;
    line-height: 1.5;
  }}
</style>
</head>
<body><div class="wrap">

<header>
  <h1>orchestrator API</h1>
  <span class="crumb">v2.4 · localhost-only</span>
  <span class="home"><a href="/dashboard">← back to dashboard</a></span>
</header>

<p class="lead">
  HTTP-JSON API exposed by <code>orchestrator.py serve</code>. All endpoints return JSON
  (except <code>/dashboard</code> and <code>/docs</code>). Requests from <code>127.0.0.1</code>
  / <code>::1</code> bypass auth by default; remote requests need
  <code>Authorization: Bearer &lt;token&gt;</code> (token at <code>~/.catalog_state/auth_token</code>).
  Set <code>ORCHESTRATOR_REQUIRE_AUTH=1</code> to require auth even on localhost.
</p>

<p class="lead">
  Curl example:
  <br>
  <code>curl -s http://127.0.0.1:9100/api/instances | jq</code>
  <br>
  <code>curl -s -X POST http://127.0.0.1:9100/api/job -H 'Content-Type: application/json' -d '{{"template_id":"flux_schnell","prompt":"a parrot"}}'</code>
</p>

{body}

<h2 class="section">Family palette</h2>
<p class="lead">Used by <code>/api/model-graph</code> to color-code workflow ↔ model edges.</p>
<div class="palette">
  {palette_html}
</div>

<footer>
  Source: <code>orchestrator.py</code> · this page is generated from the <code>API_DOCS</code> table.
  <br>
  JSON spec: <a href="/api/docs">/api/docs</a>
</footer>

</div></body></html>"""


def html_escape(s) -> str:
    """Lightweight HTML escape for the docs page."""
    return (str(s)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
            .replace("'", "&#39;"))


def _read_dashboard_html(token: str = "") -> bytes:
    """Serve the dashboard HTML with the auth token pre-injected so the user
    never has to copy-paste it. The injected `window.ORCHESTRATOR_TOKEN` is
    picked up by the dashboard JS at boot.
    """
    if not DASHBOARD_HTML_PATH.exists():
        return b"<h1>Dashboard HTML missing</h1>"
    html = DASHBOARD_HTML_PATH.read_text(encoding="utf-8")
    if token:
        injection = (
            "<script>window.ORCHESTRATOR_TOKEN = "
            + json.dumps(token)
            + ";</script>\n"
        )
        # Inject right before </head> if present, else at the start of <body>
        if "</head>" in html:
            html = html.replace("</head>", injection + "</head>", 1)
        elif "<body" in html:
            html = html.replace("<body", injection + "<body", 1)
        else:
            html = injection + html
    return html.encode("utf-8")


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
            return True  # auth disabled (--no-auth)
        # Localhost is implicitly trusted — the orchestrator is local-only.
        # If you ever expose it to a network, set ORCHESTRATOR_REQUIRE_AUTH=1
        # to disable this bypass.
        client = self.client_address[0] if self.client_address else ""
        if client in ("127.0.0.1", "::1") and not os.environ.get("ORCHESTRATOR_REQUIRE_AUTH"):
            return True
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
        # Public: dashboard HTML — token auto-injected (no manual paste)
        if self.path in ("/", "/dashboard"):
            html = _read_dashboard_html(self.expected_token)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)
            return

        # Public: API docs page (HTML)
        if self.path in ("/docs", "/docs/"):
            html = _render_docs_html().encode("utf-8")
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
            # Workflow ↔ model bipartite graph
            if self.path == "/api/model-graph":
                return self._json(compute_model_graph())

            # Upgrade suggestions: workflows using an older model variant
            # whose newer variant is in the catalog
            if self.path == "/api/upgrades":
                return self._json(compute_upgrades())

            # API docs as JSON (also rendered as HTML at /docs)
            if self.path == "/api/docs":
                return self._json({"endpoints": API_DOCS,
                                    "families_palette": FAMILY_COLORS,
                                    "version": "2.4"})

            # Live event stream — long-poll style: GET /api/events?since=<id>
            # Returns events with id > since (last 200 max). Empty list if no
            # new events in the buffer; client polls every ~1s.
            if self.path.startswith("/api/events"):
                qs = urllib.parse.urlparse(self.path).query
                params = urllib.parse.parse_qs(qs)
                try:
                    since = int(params.get("since", ["0"])[0])
                except ValueError:
                    since = 0
                evs = get_events_since(since)
                last_id = evs[-1]["id"] if evs else since
                return self._json({"events": evs, "last_id": last_id, "buffer_size": len(EVENT_LOG)})

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

        # Cancel a running command : POST /api/command/cancel/<run_id>
        if self.path.startswith("/api/command/cancel/"):
            cmd_id = self.path[len("/api/command/cancel/"):]
            with COMMAND_LOCK:
                info = COMMAND_PROCESSES.get(cmd_id)
            if not info:
                return self._json({"error": "unknown run"}, status=404)
            if info.get("status") != "running":
                return self._json({"ok": True, "already": info.get("status")})
            pid = info.get("pid")
            if not pid:
                return self._json({"error": "no pid"}, status=500)
            try:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                                    capture_output=True, timeout=10)
                else:
                    import signal as _sig
                    try: os.kill(pid, _sig.SIGTERM)
                    except ProcessLookupError: pass
                    time.sleep(0.5)
                    try: os.kill(pid, _sig.SIGKILL)
                    except ProcessLookupError: pass
                with COMMAND_LOCK:
                    if cmd_id in COMMAND_PROCESSES:
                        COMMAND_PROCESSES[cmd_id]["status"] = "cancelled"
                        COMMAND_PROCESSES[cmd_id]["finished_at"] = time.time()
                log_event("warn", "command", f"cancelled: {cmd_id} (pid={pid})", run_id=cmd_id)
                return self._json({"ok": True, "id": cmd_id, "killed_pid": pid})
            except Exception as exc:
                return self._json({"error": str(exc)}, status=500)

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
