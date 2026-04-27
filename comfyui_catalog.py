#!/usr/bin/env python
"""
ComfyUI Catalog Manager — outil unifié pour scaler une installation ComfyUI.

Sous-commandes :
    build              Construit le manifest des modèles (latest + ≤ 2 ans).
    report             Affiche le rapport (sans rien télécharger).
    download           Lance le téléchargement de la sélection via l'extension
                       comfyui-workflow-manager (avec mécanisme .partial).
    status             État courant des téléchargements.
    install-workflows  Copie tous les workflows locaux dans user/default/workflows/.
    list-no-url        Liste les workflows qui n'embarquent pas d'URLs de modèles
                       (à traiter à la main).

Usage :
    python comfyui_catalog.py build --budget 1024 --max-age-years 2
    python comfyui_catalog.py report
    python comfyui_catalog.py download
    python comfyui_catalog.py install-workflows
    python comfyui_catalog.py list-no-url

Le manifest et l'état sont sauvegardés dans :
    <repo>/.catalog_state/  (override via $COMFYUI_STATE_DIR)
"""

from __future__ import annotations
import sys
sys.stdout.reconfigure(encoding="utf-8")

import argparse
import json
import os
import re
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

# ── Config defaults ────────────────────────────────────────────────────────
DEFAULT_COMFYUI_API = "http://127.0.0.1:8188"
DEFAULT_COMFYUI_PATH = Path(os.environ.get("COMFYUI_PATH",
    str(Path(__file__).resolve().parent.parent / "ComfyUI")))
STATE_DIR = Path(os.environ.get("COMFYUI_STATE_DIR",
    str(Path(__file__).resolve().parent.parent / ".catalog_state")))
STATE_DIR.mkdir(exist_ok=True)

MANIFEST_FILE = STATE_DIR / "manifest.json"
DOWNLOAD_LIST = STATE_DIR / "download_list.json"
WORKFLOWS_NO_URL = STATE_DIR / "workflows_without_urls.txt"
LAST_SYNC_FILE = STATE_DIR / "last_sync.json"
# Cache des 239 modèles (avec sizes + dates) pour preview rapide
ALL_MODELS_CACHE = STATE_DIR / "all_models_cache.json"


# ── Family classifier (shared with extension's __init__.py) ────────────────
def model_family(name: str, url: str = "") -> tuple[str, tuple, str, str]:
    n = (name + " " + url).lower()
    role = "checkpoint"

    if "wan" in n:
        if "vae" in n: role = "vae"
        elif "lora" in n or "lightning" in n: role = "lora"
        elif "clip" in n or "umt5" in n: role = "text_encoder"
        else: role = "diffusion_model"
        if re.search(r"wan2[._]?2", n): return ("wan_video", (2, 2), "2.2", role)
        if re.search(r"wan2[._]?1", n) or "vace" in n: return ("wan_video", (2, 1), "2.1", role)
        return ("wan_video", (1, 0), "1.x", role)

    if "ltx" in n:
        if "vae" in n: role = "vae"
        elif "lora" in n: role = "lora"
        elif "clip" in n or "t5" in n: role = "text_encoder"
        else: role = "diffusion_model"
        if re.search(r"ltx2[._]?3|ltxv2[._]?3|ltx-2.3", n): return ("ltx_video", (2, 3), "2.3", role)
        if re.search(r"ltx2|ltx-v2", n): return ("ltx_video", (2, 0), "2.0", role)
        return ("ltx_video", (1, 0), "1.x", role)

    if "flux" in n:
        if "vae" in n or "ae.safetensors" in n: role = "vae"
        elif "lora" in n: role = "lora"
        elif "clip" in n or "t5" in n: role = "text_encoder"
        else: role = "diffusion_model"
        if re.search(r"flux[_.\-]?2|flux2", n):
            sub = "klein" if "klein" in n else "main"
            return (f"flux2_{sub}", (2, 0), "2", role)
        if "kontext" in n: return ("flux_kontext", (1, 0), "1", role)
        if "schnell" in n: return ("flux_schnell", (1, 0), "1", role)
        if "krea" in n: return ("flux_krea", (1, 0), "1", role)
        if "redux" in n: return ("flux_redux", (1, 0), "1", role)
        if "fill" in n: return ("flux_fill", (1, 0), "1", role)
        if "canny" in n or "depth" in n: return ("flux_controlnet", (1, 0), "1", role)
        if "uso" in n: return ("flux_uso", (1, 0), "1", role)
        return ("flux_dev", (1, 0), "1", role)

    if "qwen" in n:
        if "vae" in n: role = "vae"
        elif "lora" in n or "lightning" in n or "angles" in n: role = "lora"
        elif "vl" in n or re.search(r"qwen[_-]?3", n): role = "text_encoder"
        else: role = "diffusion_model"
        if "edit" in n or "image-edit" in n:
            if "2512" in n: return ("qwen_image_edit", (25, 12), "2512", role)
            if "2511" in n: return ("qwen_image_edit", (25, 11), "2511", role)
            if "2509" in n: return ("qwen_image_edit", (25, 9), "2509", role)
            return ("qwen_image_edit", (1, 0), "v1", role)
        if "image" in n and "vl" not in n:
            if "2512" in n: return ("qwen_image", (25, 12), "2512", role)
            return ("qwen_image", (1, 0), "v1", role)
        return ("qwen_text_encoder", (1, 0), "1", role)

    if re.search(r"sd3[._]?5", n): return ("sd3_5", (3, 5), "3.5", "diffusion_model")
    if "sdxl" in n: return ("sdxl", (1, 0), "1", "checkpoint")
    if "hunyuan" in n:
        if "video" in n:
            if "1.5" in n: return ("hunyuan_video", (1, 5), "1.5", "diffusion_model")
            return ("hunyuan_video", (1, 0), "1", "diffusion_model")
        if "3d" in n:
            if "2.1" in n: return ("hunyuan3d", (2, 1), "2.1", "checkpoint")
            return ("hunyuan3d", (2, 0), "2", "checkpoint")
    if "ace" in n and ("audio" in n or "step" in n):
        if "1.5" in n or "1_5" in n: return ("audio_ace", (1, 5), "1.5", "diffusion_model")
        return ("audio_ace", (1, 0), "1", "diffusion_model")
    if "hidream" in n:
        if "e1_1" in n: return ("hidream_e1", (1, 1), "1.1", "diffusion_model")
        if "e1" in n: return ("hidream_e1", (1, 0), "1", "diffusion_model")
        return ("hidream_i1", (1, 0), "1", "diffusion_model")
    if "chroma" in n: return ("chroma", (1, 0), "1", "diffusion_model")
    if "lumina" in n: return ("lumina", (1, 0), "1", "checkpoint")
    if "kandinsky" in n: return ("kandinsky", (5, 0), "5", "diffusion_model")
    if "z_image" in n or "z-image" in n: return ("z_image", (1, 0), "1", "diffusion_model")
    if "ovis" in n: return ("ovis", (1, 0), "1", "diffusion_model")
    if "ernie" in n: return ("ernie", (1, 0), "1", "diffusion_model")
    if "longcat" in n: return ("longcat", (1, 0), "1", "diffusion_model")
    if "omnigen" in n: return ("omnigen", (2, 0), "2", "diffusion_model")
    if "anima" in n: return ("anima", (1, 0), "1", "diffusion_model")
    if "capybara" in n: return ("capybara", (1, 0), "1", "diffusion_model")
    if "lotus" in n: return ("lotus", (1, 0), "1", "diffusion_model")
    if "infinitetalk" in n: return ("infinitetalk", (1, 0), "1", "diffusion_model")
    if "humo" in n: return ("humo", (1, 0), "1", "diffusion_model")
    if "chrono_edit" in n: return ("chrono_edit", (1, 0), "1", "diffusion_model")
    if "firered" in n: return ("firered", (1, 0), "1", "diffusion_model")

    if "vae" in n: return ("aux_vae", (0, 0), "?", "vae")
    if "clip" in n or "t5" in n or "umt5" in n: return ("aux_text_encoder", (0, 0), "?", "text_encoder")
    if "lora" in n: return ("aux_lora", (0, 0), "?", "lora")
    if "controlnet" in n: return ("aux_controlnet", (0, 0), "?", "controlnet")
    if "upscale" in n or "esrgan" in n: return ("aux_upscaler", (0, 0), "?", "upscale_models")

    return ("other", (0, 0), "?", "checkpoint")


def fmt_bytes(n: int) -> str:
    if n == 0: return "?"
    if n < 1024: return f"{n}B"
    if n < 1024**2: return f"{n/1024:.0f}KB"
    if n < 1024**3: return f"{n/1024/1024:.0f}MB"
    return f"{n/1024/1024/1024:.2f}GB"


# ── HEAD info: size + repo lastModified ────────────────────────────────────
_HF_API_CACHE: dict[str, datetime | None] = {}


def _hf_repo_from_url(url: str) -> str | None:
    m = re.match(r"https?://(?:huggingface\.co|hf\.co)/([^/]+/[^/]+)/(?:resolve|raw|blob)/", url)
    return m.group(1) if m else None


def _hf_repo_last_modified(repo: str, timeout: int = 10):
    if repo in _HF_API_CACHE:
        return _HF_API_CACHE[repo]
    try:
        req = urllib.request.Request(
            f"https://huggingface.co/api/models/{repo}",
            headers={"User-Agent": "comfyui-catalog/1.0"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
        lm = data.get("lastModified")
        if lm:
            try:
                dt = datetime.fromisoformat(lm.replace("Z", "+00:00"))
                _HF_API_CACHE[repo] = dt
                return dt
            except Exception:
                pass
    except Exception:
        pass
    _HF_API_CACHE[repo] = None
    return None


def head_info(url: str, timeout: int = 10):
    size = 0
    try:
        req = urllib.request.Request(
            url, method="HEAD",
            headers={"User-Agent": "comfyui-catalog/1.0"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            cl = resp.headers.get("content-length")
            size = int(cl) if cl else 0
    except Exception:
        pass
    repo = _hf_repo_from_url(url)
    dt = _hf_repo_last_modified(repo, timeout=timeout) if repo else None
    return size, dt


# ── Workflow scanning ──────────────────────────────────────────────────────
def load_local_workflows():
    """Returns list of (template_id, workflow_dict, json_path)."""
    try:
        from comfyui_workflow_templates_core.loader import load_manifest, get_asset_path
    except ImportError:
        print("ERREUR: comfyui_workflow_templates_core non installé.", file=sys.stderr)
        sys.exit(1)

    manifest = load_manifest()
    locals_only = [t for t in manifest.templates.values()
                   if t.bundle != "media-api" and not t.template_id.startswith("api_")]

    out = []
    for t in locals_only:
        try:
            json_path = get_asset_path(t.template_id, t.template_id + ".json")
            with open(json_path, encoding="utf-8") as f:
                wf = json.load(f)
            out.append((t.template_id, wf, json_path))
        except Exception:
            continue
    return out


def extract_models(workflow) -> list[dict]:
    out = []
    if not isinstance(workflow, dict):
        return out

    def walk(node):
        if not isinstance(node, dict):
            return
        arr = node.get("properties", {}).get("models", [])
        if isinstance(arr, list):
            for mm in arr:
                if isinstance(mm, dict) and mm.get("name"):
                    out.append(mm)

    nodes = workflow.get("nodes")
    if isinstance(nodes, list):
        for n in nodes:
            walk(n)
    defs = workflow.get("definitions")
    if isinstance(defs, dict):
        for sg in (defs.get("subgraphs") or []):
            if isinstance(sg, dict):
                for n in (sg.get("nodes") or []):
                    walk(n)
    return out


# ── Subcommands ────────────────────────────────────────────────────────────

def cmd_build(args):
    """Build the manifest with size + date filtering + budget enforcement."""
    print(f"⚙  Building catalog (budget={args.budget}GB, max_age={args.max_age_years}y)...")

    workflows = load_local_workflows()
    print(f"   Templates locaux : {len(workflows)}")

    # Aggregate unique models
    unique = {}  # url → entry
    no_url_workflows = []

    for tid, wf, jp in workflows:
        models = extract_models(wf)
        if not models:
            no_url_workflows.append(tid)
            continue
        for mm in models:
            name = mm.get("name", "")
            url = mm.get("url", "")
            if not url:
                continue
            if url not in unique:
                fam, ver, label, role = model_family(name, url)
                unique[url] = {
                    "name": name,
                    "url": url,
                    "family": fam,
                    "version": list(ver),
                    "version_label": label,
                    "role": role,
                    "directory": mm.get("directory", "?"),
                    "used_in": [],
                }
            unique[url]["used_in"].append(tid)

    print(f"   Modèles uniques  : {len(unique)}")
    print(f"   Workflows sans URL embarquée : {len(no_url_workflows)}")

    # Latest version per family
    family_max = {}
    for d in unique.values():
        fam = d["family"]
        v = tuple(d["version"])
        if v > family_max.get(fam, (0, 0)):
            family_max[fam] = v

    for d in unique.values():
        max_v = family_max.get(d["family"], (0, 0))
        d["is_latest"] = (tuple(d["version"]) == max_v)

    # Helpers used during selection later
    import re as _re
    def _extract_size_b(name: str) -> float:
        for m in _re.finditer(r"(\d+(?:\.\d+)?)[bB](?![a-zA-Z])", name):
            try: return float(m.group(1))
            except ValueError: continue
        return 0.0

    def _variant_key(name: str) -> str:
        n = name.lower()
        n = _re.sub(r"\d+(?:\.\d+)?b\b", "*", n)
        n = _re.sub(r"fp8(?:_[a-z0-9]+)?|bf16|fp16|fp32|int8|q[0-9]+_[a-z0-9]+", "*", n)
        n = _re.sub(r"[\-_]?distilled[\-_]?", "*", n)
        return n

    # HEAD info (size + date)
    print(f"   Fetching HEAD + HF API for {len(unique)} URLs...")
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=20) as pool:
        fut2url = {pool.submit(head_info, u): u for u in unique.keys()}
        done = 0
        for fut in as_completed(fut2url):
            u = fut2url[fut]
            size, dt = fut.result()
            unique[u]["size"] = size
            unique[u]["last_modified"] = dt.isoformat() if dt else None
            done += 1
            if done % 50 == 0:
                print(f"     {done}/{len(unique)}")
    print(f"   ✓ done in {time.time() - t0:.1f}s")

    # Filter: too old
    NOW = datetime.now(timezone.utc)
    cutoff = NOW - timedelta(days=args.max_age_years * 365)
    for d in unique.values():
        lm = datetime.fromisoformat(d["last_modified"]) if d["last_modified"] else None
        d["too_old"] = bool(lm and lm < cutoff)

    # Selection: latest version + not too old + biggest variant (no distilled)
    candidates = []
    for d in unique.values():
        if d["too_old"]:
            continue
        if tuple(d["version"]) == (0, 0) or d["is_latest"]:
            candidates.append(d)

    # Group by (family, role, variant_key) and keep ONLY biggest non-distilled
    by_grp = {}
    for d in candidates:
        key = (d["family"], d["role"], _variant_key(d["name"]))
        by_grp.setdefault(key, []).append(d)

    selected = []
    dropped_variants = []
    for items in by_grp.values():
        if len(items) == 1:
            selected.append(items[0]); continue
        def _score(d):
            n = d["name"].lower()
            return (
                0 if "distilled" in n else 100,   # non-distilled wins
                _extract_size_b(n),               # biggest param count wins
                d["size"],                        # bigger file wins (bf16 > fp8)
            )
        items.sort(key=_score, reverse=True)
        selected.append(items[0])
        for x in items[1:]:
            x["dropped_reason"] = f"smaller/distilled (kept: {items[0]['name']})"
            dropped_variants.append(x)
    if dropped_variants:
        total_dropped = sum(x["size"] for x in dropped_variants)
        print(f"   📉 Dropped {len(dropped_variants)} smaller/distilled variants "
              f"({fmt_bytes(total_dropped)})")

    selected.sort(key=lambda x: x["last_modified"] or "", reverse=True)

    selected_size = sum(d["size"] for d in selected)
    budget_bytes = args.budget * 1024**3

    # Budget enforcement: if over, drop heaviest models from less-used families
    if selected_size > budget_bytes:
        print(f"   ⚠  Selection {fmt_bytes(selected_size)} > budget {fmt_bytes(budget_bytes)}")
        # Sort by usage frequency (descending) then by size (ascending)
        # Keep most-used + smallest first; drop biggest unused last
        for d in selected:
            d["_usage_score"] = len(d["used_in"]) * 1_000_000_000 - d["size"]
        selected.sort(key=lambda x: x["_usage_score"], reverse=True)
        kept = []
        running = 0
        dropped = []
        for d in selected:
            if running + d["size"] <= budget_bytes:
                kept.append(d)
                running += d["size"]
            else:
                dropped.append(d)
        for d in kept:
            del d["_usage_score"]
        for d in dropped:
            del d["_usage_score"]
        print(f"   → Kept {len(kept)} ({fmt_bytes(running)}) · Dropped {len(dropped)} ({fmt_bytes(sum(d['size'] for d in dropped))})")
        selected = kept
        selected_size = running

    # Build manifest
    manifest = {
        "generated_at": NOW.isoformat(),
        "budget_gb": args.budget,
        "max_age_years": args.max_age_years,
        "selection_size_bytes": selected_size,
        "selection_count": len(selected),
        "total_unique_models": len(unique),
        "selected_models": selected,
        "no_url_workflows": no_url_workflows,
    }
    MANIFEST_FILE.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    # Cache TOUS les modèles (avec leurs metadata) pour /api/preview rapide
    all_cache = {
        "generated_at": NOW.isoformat(),
        "models": list(unique.values()),
    }
    ALL_MODELS_CACHE.write_text(json.dumps(all_cache, indent=2, default=str), encoding="utf-8")

    # Write download_list separately for quick access
    download_list = [
        {"name": d["name"], "url": d["url"], "directory": d["directory"], "size": d["size"]}
        for d in selected
    ]
    DOWNLOAD_LIST.write_text(json.dumps(download_list, indent=2), encoding="utf-8")

    # Write no-url workflows for manual review
    WORKFLOWS_NO_URL.write_text("\n".join(sorted(no_url_workflows)), encoding="utf-8")

    print()
    print(f"✅ Manifest: {MANIFEST_FILE}")
    print(f"✅ Download list ({len(download_list)} files, {fmt_bytes(selected_size)}): {DOWNLOAD_LIST}")
    print(f"✅ Workflows sans URL ({len(no_url_workflows)}): {WORKFLOWS_NO_URL}")


def cmd_report(args):
    """Affiche un rapport résumé du manifest."""
    if not MANIFEST_FILE.exists():
        print("Aucun manifest. Lance d'abord `build`.", file=sys.stderr)
        sys.exit(1)

    manifest = json.loads(MANIFEST_FILE.read_text(encoding="utf-8"))
    selected = manifest["selected_models"]
    total = sum(d["size"] for d in selected)

    print(f"📊 Manifest généré le {manifest['generated_at']}")
    print(f"   Budget          : {manifest['budget_gb']} GB")
    print(f"   Max age         : {manifest['max_age_years']} ans")
    print(f"   Sélection       : {manifest['selection_count']} modèles, {fmt_bytes(total)}")
    print(f"   Workflows sans URL : {len(manifest['no_url_workflows'])}")
    print()

    # Group by family
    by_family = {}
    for d in selected:
        by_family.setdefault(d["family"], []).append(d)
    print(f"{'FAMILLE':25} {'#':>4} {'TAILLE':>10}")
    print("-" * 45)
    for fam in sorted(by_family.keys()):
        items = by_family[fam]
        s = sum(d["size"] for d in items)
        print(f"{fam:25} {len(items):>4} {fmt_bytes(s):>10}")
    print("-" * 45)
    print(f"{'TOTAL':25} {len(selected):>4} {fmt_bytes(total):>10}")


def cmd_download(args):
    """Lance les téléchargements via l'extension comfyui-workflow-manager."""
    if not DOWNLOAD_LIST.exists():
        print("Aucune liste de téléchargement. Lance d'abord `build`.", file=sys.stderr)
        sys.exit(1)

    # Precheck — ComfyUI must be running for the workflow-manager extension to
    # accept POSTs. Without this, every item below would time out for 30 s
    # before failing, and the user sees nothing for minutes.
    print(f"Verifying ComfyUI is reachable at {args.api}...")
    try:
        with urllib.request.urlopen(f"{args.api}/queue", timeout=3) as resp:
            resp.read(64)
        print(f"  OK -- ComfyUI online")
    except Exception as exc:
        print()
        print("=" * 70, file=sys.stderr)
        print(f"ERREUR: ComfyUI n'est pas joignable a {args.api}", file=sys.stderr)
        print(f"  details: {exc}", file=sys.stderr)
        print(file=sys.stderr)
        print("Le download a besoin de l'extension comfyui-workflow-manager,", file=sys.stderr)
        print("qui n'est dispo que quand ComfyUI tourne.", file=sys.stderr)
        print(file=sys.stderr)
        print("Lance ComfyUI d'abord, par exemple via l'orchestrateur :", file=sys.stderr)
        print("    POST /api/command  id=launch_comfyui", file=sys.stderr)
        print("ou directement :", file=sys.stderr)
        print(f"    cd <comfyui_path> && python main.py --port 8188", file=sys.stderr)
        print("=" * 70, file=sys.stderr)
        sys.exit(1)
    print()

    items = json.loads(DOWNLOAD_LIST.read_text(encoding="utf-8"))
    print(f"Lancement de {len(items)} telechargements via {args.api}/workflow-manager/download-model")
    print()

    enqueued = 0
    skipped = 0
    errors = 0
    for i, item in enumerate(items, 1):
        # Check if already on disk via the integrity API (auto-skip if valid)
        target_dir = args.comfyui_path / "models" / item["directory"]
        target_path = target_dir / item["name"]
        if target_path.exists() and target_path.stat().st_size == item["size"] and item["size"] > 0:
            print(f"  [{i}/{len(items)}] ⊖ {item['name']} (déjà présent, taille OK)")
            skipped += 1
            continue

        # POST to download endpoint
        body = json.dumps({
            "filename": item["name"],
            "folder": item["directory"],
            "url": item["url"],
        }).encode()
        try:
            req = urllib.request.Request(
                f"{args.api}/workflow-manager/download-model",
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
            if "error" in data:
                print(f"  [{i}/{len(items)}] ✗ {item['name']}: {data['error']}")
                errors += 1
            else:
                print(f"  [{i}/{len(items)}] → {item['name']} ({fmt_bytes(item['size'])}) [id={data.get('download_id')}]")
                enqueued += 1
        except Exception as exc:
            print(f"  [{i}/{len(items)}] ✗ {item['name']}: {exc}")
            errors += 1

        # Light pacing — avoid stampeding the connection limit
        time.sleep(0.05)

    print()
    print(f"📊 {enqueued} mis en file · {skipped} déjà OK · {errors} erreurs")
    print(f"   Surveille la progression : {args.api}/workflow-manager/downloads")


def cmd_status(args):
    """Statut des downloads."""
    try:
        with urllib.request.urlopen(f"{args.api}/workflow-manager/downloads", timeout=5) as resp:
            data = json.loads(resp.read())
    except Exception as exc:
        print(f"Impossible de joindre {args.api}: {exc}", file=sys.stderr)
        sys.exit(1)

    downloads = data.get("downloads", [])
    if not downloads:
        print("Aucun téléchargement en cours.")
        return

    by_status = {}
    for d in downloads:
        by_status.setdefault(d["status"], []).append(d)

    for status in ["downloading", "pending", "complete", "error"]:
        items = by_status.get(status, [])
        if not items:
            continue
        print(f"\n[{status}] ({len(items)}):")
        for d in items:
            line = f"  {d['filename'][:60]:60} {d['progress']:>3}%"
            if d.get("error"):
                line += f"  err: {d['error'][:40]}"
            print(line)


def cmd_sync(args):
    """Update the comfyui_workflow_templates package then rebuild the manifest.
    Permet de récupérer les nouveaux templates / modèles ajoutés en amont."""
    import subprocess as _sp
    print("🔄 Mise à jour de comfyui_workflow_templates_core...")
    try:
        _sp.check_call([sys.executable, "-m", "pip", "install", "-U", "--quiet",
                         "comfyui_workflow_templates_core",
                         "comfyui_workflow_templates_media_image",
                         "comfyui_workflow_templates_media_video",
                         "comfyui_workflow_templates_media_other"])
    except Exception as exc:
        print(f"   ⚠ pip update échoué: {exc}")

    # Reload manifest module to pick up changes
    print("🔄 Rebuild du manifest...")
    cmd_build(args)


def cmd_cleanup(args):
    """Supprime du disque les modèles qui ne sont plus dans le manifest courant.
    Permet de récupérer de l'espace après un rebuild qui drop d'anciennes versions.
    """
    if not DOWNLOAD_LIST.exists():
        print("Pas de download_list — lance `build` d'abord", file=sys.stderr)
        sys.exit(1)
    keep = json.loads(DOWNLOAD_LIST.read_text(encoding="utf-8"))
    keep_set = set((it["directory"], it["name"].lower()) for it in keep)

    models_root = args.comfyui_path / "models"
    if not models_root.exists():
        print(f"models/ absent: {models_root}", file=sys.stderr)
        sys.exit(1)

    to_delete = []
    total_size = 0
    for folder in models_root.iterdir():
        if not folder.is_dir():
            continue
        # Walk + check matching
        for f in folder.rglob("*"):
            if not f.is_file():
                continue
            ext = f.suffix.lower()
            if ext not in (".safetensors", ".ckpt", ".pt", ".bin", ".pth", ".gguf", ".sft"):
                continue
            key = (folder.name, f.name.lower())
            # Some models are in subfolders mapped under the same logical folder.
            # We only delete exact matches that are NOT in the keep set
            if key not in keep_set:
                to_delete.append(f)
                total_size += f.stat().st_size

    if not to_delete:
        print("✅ Rien à nettoyer — tous les modèles sur disque sont dans le manifest")
        return

    print(f"🗑  {len(to_delete)} fichier(s) obsolète(s) ({total_size/1024**3:.1f} GB) :")
    for f in to_delete[:20]:
        print(f"   - {f.relative_to(models_root)} ({f.stat().st_size/1024**3:.2f} GB)")
    if len(to_delete) > 20:
        print(f"   ... +{len(to_delete)-20} autres")

    if args.dry_run:
        print("\n[dry-run] aucune suppression effectuée (utilise sans --dry-run)")
        return

    if not args.yes:
        ans = input("\nSupprimer ? [yes/N] ").strip().lower()
        if ans not in ("yes", "y"):
            print("Annulé.")
            return

    deleted = 0
    for f in to_delete:
        try:
            f.unlink()
            deleted += 1
        except Exception as exc:
            print(f"   ✗ {f.name}: {exc}")
    print(f"✅ {deleted}/{len(to_delete)} fichiers supprimés ({total_size/1024**3:.1f} GB libérés)")


def simulate_selection(budget_gb: int, max_age_years: int) -> dict:
    """Simule le résultat d'un build sans toucher au disque.
    Lit le cache `all_models_cache.json` (créé lors du dernier build).
    Renvoie ce qui SERA sélectionné + diff vs manifest courant.
    """
    if not ALL_MODELS_CACHE.exists():
        return {"error": "Lance d'abord `build` au moins une fois pour générer le cache"}

    cache = json.loads(ALL_MODELS_CACHE.read_text(encoding="utf-8"))
    models = cache.get("models", [])

    # Re-apply too_old filter
    NOW = datetime.now(timezone.utc)
    cutoff = NOW - timedelta(days=max_age_years * 365)
    for d in models:
        lm_str = d.get("last_modified")
        if lm_str:
            try:
                lm = datetime.fromisoformat(lm_str)
                d["too_old"] = (lm < cutoff)
            except Exception:
                d["too_old"] = False
        else:
            d["too_old"] = False

    # Latest version per family
    family_max = {}
    for d in models:
        fam = d["family"]
        v = tuple(d["version"])
        if v > family_max.get(fam, (0, 0)):
            family_max[fam] = v
    for d in models:
        d["is_latest"] = (tuple(d["version"]) == family_max.get(d["family"], (0, 0)))

    # Biggest variant filter (mirror cmd_build logic)
    import re as _re
    def _size_b(name):
        for m in _re.finditer(r"(\d+(?:\.\d+)?)[bB](?![a-zA-Z])", name):
            try: return float(m.group(1))
            except ValueError: continue
        return 0.0

    def _vkey(name):
        n = name.lower()
        n = _re.sub(r"\d+(?:\.\d+)?b\b", "*", n)
        n = _re.sub(r"fp8(?:_[a-z0-9]+)?|bf16|fp16|fp32|int8|q[0-9]+_[a-z0-9]+", "*", n)
        n = _re.sub(r"[\-_]?distilled[\-_]?", "*", n)
        return n

    candidates = [d for d in models
                  if not d["too_old"] and (tuple(d["version"]) == (0, 0) or d["is_latest"])]
    by_grp = {}
    for d in candidates:
        by_grp.setdefault((d["family"], d["role"], _vkey(d["name"])), []).append(d)

    selected = []
    for items in by_grp.values():
        if len(items) == 1:
            selected.append(items[0]); continue
        items.sort(key=lambda d: (
            0 if "distilled" in d["name"].lower() else 100,
            _size_b(d["name"]),
            d.get("size", 0),
        ), reverse=True)
        selected.append(items[0])

    selected_size = sum(d.get("size", 0) for d in selected)
    budget_bytes = budget_gb * 1024**3

    if selected_size > budget_bytes:
        for d in selected:
            d["_score"] = len(d.get("used_in", [])) * 1_000_000_000 - d.get("size", 0)
        selected.sort(key=lambda x: x["_score"], reverse=True)
        kept, running = [], 0
        for d in selected:
            if running + d.get("size", 0) <= budget_bytes:
                kept.append(d); running += d.get("size", 0)
        for d in kept: d.pop("_score", None)
        selected = kept

    # Compute diff vs current manifest
    current = set()
    if MANIFEST_FILE.exists():
        try:
            cur = json.loads(MANIFEST_FILE.read_text(encoding="utf-8"))
            current = {m["url"] for m in cur.get("selected_models", [])}
        except Exception:
            pass
    new_urls = {d["url"] for d in selected}
    added = [d for d in selected if d["url"] not in current]
    removed_urls = current - new_urls
    # Categorize by family for the preview
    by_family = {}
    for d in selected:
        by_family.setdefault(d["family"], []).append(d)
    family_summary = sorted([
        {"family": fam, "count": len(items),
         "size_gb": round(sum(d.get("size", 0) for d in items) / 1024**3, 2)}
        for fam, items in by_family.items()
    ], key=lambda x: -x["size_gb"])

    return {
        "budget_gb": budget_gb,
        "max_age_years": max_age_years,
        "selection_count": len(selected),
        "selection_size_gb": round(sum(d.get("size", 0) for d in selected) / 1024**3, 2),
        "added_count": len(added),
        "added_size_gb": round(sum(d.get("size", 0) for d in added) / 1024**3, 2),
        "removed_count": len(removed_urls),
        "by_family": family_summary,
    }


def cmd_apply(args):
    """Macro : build → download → cleanup en séquence.
    Pour appliquer un changement de budget/bundle en un clic."""
    print(f"✨ Apply changes (budget={args.budget} GB, max_age={args.max_age_years}y)")
    print()

    print("=== 1/3 BUILD ===")
    cmd_build(args)
    print()

    print("=== 2/3 DOWNLOAD ===")
    cmd_download(args)
    print()

    print("=== 3/3 CLEANUP (obsolete models) ===")
    args.dry_run = False
    args.yes = True
    cmd_cleanup(args)
    print()
    print("✅ Apply changes terminé")


def cmd_download_shard(args):
    """Télécharge UNIQUEMENT la part `shard` sur `total` du download_list.

    Stratégie de partition (équilibrée par taille) :
      - Trie tous les items par taille décroissante
      - Distribue round-robin aux N workers
      - Worker `shard` (0-indexed) prend les items où `index % total == shard`

    Cela garantit que chaque worker prend une part équivalente en bytes,
    indépendamment du nombre de fichiers.
    """
    if not DOWNLOAD_LIST.exists():
        print("download_list.json absent — lance `build` d'abord", file=sys.stderr)
        sys.exit(1)
    items = json.loads(DOWNLOAD_LIST.read_text(encoding="utf-8"))

    shard = int(args.shard)
    total = int(args.total)
    if shard < 0 or shard >= total or total < 1:
        print(f"ERREUR: shard={shard} doit être dans [0, {total - 1}]", file=sys.stderr)
        sys.exit(1)

    # Sort by size desc, then assign round-robin → balanced bytes per shard
    sorted_items = sorted(items, key=lambda x: -int(x.get("size", 0)))
    my_items = [item for i, item in enumerate(sorted_items) if i % total == shard]
    my_size = sum(int(it.get("size", 0)) for it in my_items)

    print(f"📦 Shard {shard}/{total} : {len(my_items)} fichiers · "
          f"{my_size / 1024**3:.2f} GB / {sum(int(i.get('size', 0)) for i in items) / 1024**3:.2f} GB total")
    print()

    # Download via the local extension (with .partial mechanism for safety on shared FS)
    enqueued = 0
    skipped = 0
    errors = 0
    for i, item in enumerate(my_items, 1):
        target_path = args.comfyui_path / "models" / item["directory"] / item["name"]
        if target_path.exists() and target_path.stat().st_size == item["size"] and item["size"] > 0:
            print(f"  [{i}/{len(my_items)}] ⊖ {item['name']} (déjà OK)")
            skipped += 1
            continue

        body = json.dumps({
            "filename": item["name"],
            "folder": item["directory"],
            "url": item["url"],
        }).encode()
        try:
            req = urllib.request.Request(
                f"{args.api}/workflow-manager/download-model",
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
            if "error" in data:
                print(f"  [{i}/{len(my_items)}] ✗ {item['name']}: {data['error']}")
                errors += 1
            else:
                print(f"  [{i}/{len(my_items)}] → {item['name']} ({item['size'] / 1024**3:.2f} GB) [id={data.get('download_id')}]")
                enqueued += 1
        except Exception as exc:
            print(f"  [{i}/{len(my_items)}] ✗ {item['name']}: {exc}")
            errors += 1

        time.sleep(0.05)

    print()
    print(f"📊 Shard {shard}/{total} : {enqueued} enqueued · {skipped} skip · {errors} errors")


def cmd_pool_build(args):
    """Build the symlink farm pour le mode pool.

    Concept : chaque DGX télécharge 1/N du catalogue mais voit les N/N en
    créant des symlinks vers les shards des peers (montés en NFS).

    Pour chaque modèle du download_list :
      - Si je possède le shard (round-robin par taille) → file local, pas de symlink
      - Sinon → symlink models/<folder>/<file> → /mnt/peer_dg<owner_shard>/<folder>/<file>

    Le résultat : ComfyUI sur chaque DGX voit l'intégralité du catalogue, mais
    seuls 1/N des fichiers sont vraiment sur disque local.
    """
    if not DOWNLOAD_LIST.exists():
        print("download_list.json absent — lance `build` d'abord", file=sys.stderr)
        sys.exit(1)
    items = json.loads(DOWNLOAD_LIST.read_text(encoding="utf-8"))

    total = int(args.total)
    self_shard = int(args.self)
    if self_shard < 0 or self_shard >= total:
        print(f"ERREUR: --self {self_shard} doit être dans [0, {total - 1}]", file=sys.stderr)
        sys.exit(1)

    peer_mount = args.peer_mount  # e.g. "/mnt/peer_dg{shard}"
    if "{shard}" not in peer_mount:
        print(f"ERREUR: --peer-mount doit contenir {{shard}} (ex: /mnt/peer_dg{{shard}})", file=sys.stderr)
        sys.exit(1)

    models_root = args.comfyui_path / "models"
    models_root.mkdir(parents=True, exist_ok=True)

    # Same partition algorithm than download-shard
    sorted_items = sorted(items, key=lambda x: -int(x.get("size", 0)))

    stats = {"local": 0, "linked": 0, "errors": 0, "skipped": 0}
    for i, item in enumerate(sorted_items):
        owner = i % total
        folder = item["directory"]
        name = item["name"]
        target_dir = models_root / folder
        target_dir.mkdir(parents=True, exist_ok=True)
        target_path = target_dir / name

        if owner == self_shard:
            # We own this file — leave it alone (downloader created it)
            stats["local"] += 1
            continue

        # Peer-owned : create symlink
        peer_path = Path(peer_mount.format(shard=owner)) / folder / name

        # If we already have a real file (legacy), warn and skip
        if target_path.exists() and not target_path.is_symlink():
            print(f"  ⚠ {target_path} existe localement (pas un symlink). Skip — déplace-le manuellement si redondant avec le pool.")
            stats["skipped"] += 1
            continue

        try:
            if target_path.is_symlink():
                # Update if pointing somewhere else
                if Path(os.readlink(target_path)) == peer_path:
                    stats["linked"] += 1
                    continue
                target_path.unlink()
            target_path.symlink_to(peer_path)
            stats["linked"] += 1
        except OSError as e:
            # Windows requires admin or developer mode for symlinks
            print(f"  ✗ symlink {name} → {peer_path} : {e}")
            stats["errors"] += 1

    print(f"✅ Pool build : {stats['local']} local · {stats['linked']} symlinks · "
          f"{stats['skipped']} skip · {stats['errors']} errors")
    print(f"   Self shard : {self_shard}/{total}")
    print(f"   Peer mount : {peer_mount}")


def cmd_install_workflows(args):
    """Copie tous les workflows locaux dans user/default/workflows/."""
    target_dir = args.comfyui_path / "user" / "default" / "workflows"
    target_dir.mkdir(parents=True, exist_ok=True)

    workflows = load_local_workflows()
    print(f"📁 Copie de {len(workflows)} workflows dans {target_dir}")
    copied = 0
    for tid, wf, json_path in workflows:
        dest = target_dir / f"{tid}.json"
        try:
            shutil.copy2(json_path, dest)
            copied += 1
        except Exception as exc:
            print(f"  ✗ {tid}: {exc}")
    print(f"✅ {copied} workflows copiés")


def cmd_list_no_url(args):
    """Liste les workflows sans URL embarquée."""
    if not WORKFLOWS_NO_URL.exists():
        print("Aucune liste générée. Lance d'abord `build`.", file=sys.stderr)
        sys.exit(1)

    workflows = WORKFLOWS_NO_URL.read_text(encoding="utf-8").strip().split("\n")
    workflows = [w for w in workflows if w]
    print(f"⚠  {len(workflows)} workflows sans URL embarquée (à traiter manuellement) :")
    for w in workflows:
        print(f"   • {w}")
    print()
    print("Ces workflows référencent des modèles mais ComfyUI n'embarque pas leur URL.")
    print("Pour ceux-ci, utilise l'extension workflow-manager qui consulte la base")
    print("ComfyUI-Manager (527 modèles indexés) pour résoudre l'URL automatiquement.")


# ── CLI ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--api", default=DEFAULT_COMFYUI_API,
                         help="URL de l'API ComfyUI (défaut: %(default)s)")
    parser.add_argument("--comfyui-path", type=Path, default=DEFAULT_COMFYUI_PATH,
                         help="Chemin de l'installation ComfyUI (défaut: %(default)s)")

    sp = parser.add_subparsers(dest="cmd", required=True)

    p_build = sp.add_parser("build", help="Construit le manifest")
    p_build.add_argument("--budget", type=int, default=1024,
                          help="Budget de stockage en GB (défaut: 1024 = 1 TB)")
    p_build.add_argument("--max-age-years", type=int, default=2,
                          help="Âge max des modèles en années (défaut: 2)")
    p_build.set_defaults(func=cmd_build)

    sp.add_parser("report", help="Affiche le rapport").set_defaults(func=cmd_report)
    sp.add_parser("download", help="Lance les téléchargements").set_defaults(func=cmd_download)
    sp.add_parser("status", help="État des téléchargements").set_defaults(func=cmd_status)
    sp.add_parser("install-workflows", help="Installe les workflows").set_defaults(func=cmd_install_workflows)
    sp.add_parser("list-no-url", help="Workflows sans URL").set_defaults(func=cmd_list_no_url)

    p_sync = sp.add_parser("sync", help="Update templates package + rebuild manifest")
    p_sync.add_argument("--budget", type=int, default=700)
    p_sync.add_argument("--max-age-years", type=int, default=2)
    p_sync.set_defaults(func=cmd_sync)

    p_clean = sp.add_parser("cleanup", help="Supprime les modèles obsolètes (absents du manifest)")
    p_clean.add_argument("--dry-run", action="store_true")
    p_clean.add_argument("--yes", action="store_true", help="Skip confirmation")
    p_clean.set_defaults(func=cmd_cleanup)

    p_apply = sp.add_parser("apply", help="Macro: build + download + cleanup en séquence")
    p_apply.add_argument("--budget", type=int, default=700)
    p_apply.add_argument("--max-age-years", type=int, default=2)
    p_apply.set_defaults(func=cmd_apply)

    p_shard = sp.add_parser("download-shard",
                              help="Télécharge la part N/M du download_list (cluster mode)")
    p_shard.add_argument("--shard", type=int, required=True,
                          help="Index de ce worker (0-indexed)")
    p_shard.add_argument("--total", type=int, required=True,
                          help="Nombre total de workers participant")
    p_shard.set_defaults(func=cmd_download_shard)

    p_pool = sp.add_parser("pool-build",
                            help="Build symlink farm pour mode pool (1/N local + N-1/N via NFS)")
    p_pool.add_argument("--self", type=int, required=True,
                          help="Mon shard (0-indexed)")
    p_pool.add_argument("--total", type=int, required=True,
                          help="Nombre total de DGX dans le pool")
    p_pool.add_argument("--peer-mount", default="/mnt/peer_dg{shard}",
                          help="Pattern de mount path des peers (utilise {shard})")
    p_pool.set_defaults(func=cmd_pool_build)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
