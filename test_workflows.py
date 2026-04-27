#!/usr/bin/env python
"""
Harness de test automatique pour valider les workflows ComfyUI.

Stratégie :
  Phase 1 — exécute tous les text→image avec un prompt par défaut.
  Phase 2 — exécute tous les text→audio.
  Phase 3 — exécute tous les text→video.
  Phase 4-6 — utilisent les outputs des phases précédentes comme inputs.

Le dernier output produit (image, video, audio) est mémorisé et réutilisé pour
le workflow suivant qui en a besoin → chaînage automatique.

Pour chaque workflow, génère un rapport :
  - succès / échec
  - durée
  - chemin du fichier produit
  - erreur s'il y en a

Workflows qui passent → exportés dans .catalog_state/working_workflows/

Usage :
    python test_workflows.py --phase 1
    python test_workflows.py --phase 1 --limit 5    # tester les 5 premiers
    python test_workflows.py --all                   # toutes phases dans l'ordre
    python test_workflows.py --report                # voir les résultats sans relancer
"""
from __future__ import annotations
import sys
sys.stdout.reconfigure(encoding="utf-8")

import argparse
import json
import os
import shutil
import time
import urllib.parse
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path

STATE_DIR = Path(os.environ.get("COMFYUI_STATE_DIR",
    str(Path(__file__).resolve().parent.parent / ".catalog_state")))
RESULTS_FILE = STATE_DIR / "test_results.json"
WORKING_DIR = STATE_DIR / "working_workflows"
WORKING_DIR.mkdir(exist_ok=True)

# Pool of generated outputs by type, used for chaining
LATEST_OUTPUTS = STATE_DIR / "latest_outputs.json"


DEFAULT_PROMPTS = [
    "cinematic shot of a black cat in a misty forest, 4k photorealistic",
    "a serene mountain landscape at golden hour, ultra detailed",
    "abstract geometric composition with vibrant colors",
]

# Edit-style prompt templates par catégorie : utilisés comme prompt par défaut
# pour les workflows phase 4+ qui éditent une image. Ces prompts complètent les
# images starter générées par generate_starters.py — ensemble ils forment
# un test cohérent.
EDIT_PROMPTS = {
    "portrait":     "make the person smile naturally, keep all other features identical",
    "scene":        "change the lighting to a moody overcast day, keep composition",
    "object":       "place the object on a wooden table with soft natural lighting",
    "anime":        "add subtle background details, keep character style consistent",
    "abstract":     "introduce more vibrant colors while keeping the geometric structure",
    "controlnet_pose": "render this pose as a futuristic astronaut on Mars",
    "controlnet_depth": "render this depth map as a snowy alpine village at dawn",
    "controlnet_canny": "render these line edges as a fully colored watercolor painting",
    "inpaint":      "replace the masked area with a small flower bouquet",
    "upscale":      "preserve all details while enhancing sharpness",
    "relight":      "relight from the left side with warm golden light",
    "graphic":      "add a typography title and a subtle background gradient",
    "lipsync":      "natural mouth movement in sync with the audio",
    "general":      "enhance details, sharper focus, vibrant colors",
}

STARTERS_DIR = STATE_DIR / "starters"


def fmt_dur(seconds: float) -> str:
    if seconds < 60: return f"{seconds:.1f}s"
    return f"{seconds/60:.1f}m"


# ── ComfyUI API client ──────────────────────────────────────────────────────

class ComfyClient:
    def __init__(self, base="http://127.0.0.1:8188"):
        self.base = base.rstrip("/")
        self.client_id = uuid.uuid4().hex

    def _post_json(self, path, body, timeout=30):
        req = urllib.request.Request(
            f"{self.base}{path}",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())

    def _get_json(self, path, timeout=10):
        with urllib.request.urlopen(f"{self.base}{path}", timeout=timeout) as resp:
            return json.loads(resp.read())

    def queue_prompt(self, prompt_graph: dict) -> str:
        """Returns prompt_id."""
        body = {"prompt": prompt_graph, "client_id": self.client_id}
        data = self._post_json("/prompt", body)
        return data["prompt_id"]

    def history(self, prompt_id: str):
        try:
            return self._get_json(f"/history/{prompt_id}")
        except Exception:
            return {}

    def upload_image(self, file_path: Path) -> str:
        """Upload via multipart, returns the filename in input/."""
        # Use urllib's request with multipart manually
        boundary = uuid.uuid4().hex
        body_lines = []
        body_lines.append(f"--{boundary}".encode())
        body_lines.append(b'Content-Disposition: form-data; name="image"; filename="' +
                          file_path.name.encode() + b'"')
        body_lines.append(b"Content-Type: application/octet-stream")
        body_lines.append(b"")
        body_lines.append(file_path.read_bytes())
        body_lines.append(f"--{boundary}--".encode())
        body_lines.append(b"")
        body = b"\r\n".join(body_lines)

        req = urllib.request.Request(
            f"{self.base}/upload/image",
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
        return data.get("name", file_path.name)


# ── Prompt graph manipulation ───────────────────────────────────────────────

def workflow_ui_to_api(workflow: dict) -> dict | None:
    """Convert a UI-format workflow to API format suitable for /prompt.
    UI format has 'nodes' list; API format is dict keyed by node ID with
    'class_type' and 'inputs'.

    For simplicity, we look for an 'extra' or 'extras' field that some templates
    embed; otherwise we'd have to walk links manually. ComfyUI's official
    convention: workflows in user/default/workflows/ are UI-format, but the
    /prompt endpoint requires API-format. ComfyUI's frontend does this conversion.

    For automated testing, the cleanest path is to load the workflow into the
    graph via the websocket client, then read app.graphToPrompt(). Since we
    can't run that here, we attempt a heuristic conversion that works for most
    workflows.
    """
    if not isinstance(workflow, dict):
        return None
    if "nodes" not in workflow:
        # already API format
        return workflow if all(isinstance(v, dict) and "class_type" in v
                                for v in workflow.values()) else None

    # Build node_id → links map from links list
    api = {}
    nodes = workflow.get("nodes") or []
    links = workflow.get("links") or []  # [link_id, from_node, from_slot, to_node, to_slot, type]
    link_to_endpoints = {l[0]: (l[1], l[2], l[3], l[4]) for l in links if isinstance(l, list) and len(l) >= 5}

    for n in nodes:
        if not isinstance(n, dict):
            continue
        nid = str(n.get("id"))
        ntype = n.get("type")
        if not nid or not ntype:
            continue
        # widgets_values map back to inputs by name — this requires knowledge of
        # node definitions which is complex. We provide a best-effort mapping.
        widget_values = n.get("widgets_values") or []
        node_inputs = {}
        # link inputs
        for inp in (n.get("inputs") or []):
            if not isinstance(inp, dict):
                continue
            link_id = inp.get("link")
            if link_id is None:
                continue
            ep = link_to_endpoints.get(link_id)
            if ep:
                from_node, from_slot, _to_node, _to_slot = ep
                node_inputs[inp.get("name", "?")] = [str(from_node), int(from_slot)]
        api[nid] = {
            "class_type": ntype,
            "inputs": node_inputs,
            "_meta": {"title": n.get("title") or ntype, "_widgets": widget_values},
        }
    return api


def latest_output(kind: str) -> str | None:
    """Returns path of last generated file of given kind (image/video/audio)."""
    if not LATEST_OUTPUTS.exists():
        return None
    data = json.loads(LATEST_OUTPUTS.read_text(encoding="utf-8"))
    return data.get(kind)


def set_latest_output(kind: str, path: str):
    data = {}
    if LATEST_OUTPUTS.exists():
        data = json.loads(LATEST_OUTPUTS.read_text(encoding="utf-8"))
    data[kind] = path
    LATEST_OUTPUTS.write_text(json.dumps(data, indent=2), encoding="utf-8")


# ── Test orchestration ──────────────────────────────────────────────────────

def load_classes() -> dict:
    f = STATE_DIR / "workflow_classes.json"
    if not f.exists():
        print("⚠  Pas de classification — lance d'abord `classify_workflows.py`",
              file=sys.stderr)
        sys.exit(1)
    return json.loads(f.read_text(encoding="utf-8"))


def load_results() -> dict:
    if RESULTS_FILE.exists():
        return json.loads(RESULTS_FILE.read_text(encoding="utf-8"))
    return {"runs": [], "by_workflow": {}}


def save_results(results: dict):
    RESULTS_FILE.write_text(json.dumps(results, indent=2), encoding="utf-8")


def get_starter_for_category(category: str) -> Path | None:
    """Returns path to a starter image matching the category.
    Falls back to 'general', then to any available starter."""
    candidates = [category, "general"]
    if category == "?": candidates = ["general"]
    for c in candidates:
        p = STARTERS_DIR / f"{c}.png"
        if p.exists():
            return p
    # Last resort: any starter we have
    if STARTERS_DIR.exists():
        for p in STARTERS_DIR.glob("*.png"):
            return p
    return None


def inject_workflow_inputs(client: ComfyClient, api_graph: dict, klass: dict,
                            prompt: str) -> tuple[dict, list[str]]:
    """Inject text prompt + starter image/video/audio inputs into the API graph.
    Returns (modified_graph, notes_list).
    """
    notes = []

    # 1. Inject text prompt into all CLIPTextEncode-style nodes
    for nid, node in api_graph.items():
        ct = node.get("class_type", "")
        if ct in ("CLIPTextEncode", "CLIPTextEncodeSDXL", "T5TextEncode"):
            node.setdefault("inputs", {})["text"] = prompt
        # Inject random seed into samplers
        if ct in ("KSampler", "KSamplerAdvanced", "SamplerCustom"):
            import random
            node.setdefault("inputs", {})["seed"] = random.randint(0, 2**31 - 1)

    # 2. If workflow needs an image input, upload a starter
    inputs_needed = set(klass.get("inputs") or [])
    if "image" in inputs_needed:
        starter = get_starter_for_category(klass.get("category", "general"))
        if not starter:
            # Fall back to latest output image from a previous phase run
            latest_img = latest_output("image")
            if latest_img:
                starter = Path(os.environ.get("COMFYUI_PATH",
                    str(Path(__file__).resolve().parent.parent / "ComfyUI"))) / "output" / latest_img
        if starter and starter.exists():
            try:
                uploaded_name = client.upload_image(starter)
                notes.append(f"image input: {uploaded_name} (cat={klass.get('category')})")
                # Replace LoadImage nodes' filename
                for nid, node in api_graph.items():
                    if node.get("class_type") in ("LoadImage", "LoadImageMask",
                                                    "ETN_LoadImageBase64"):
                        node.setdefault("inputs", {})["image"] = uploaded_name
            except Exception as e:
                notes.append(f"image upload failed: {e}")
        else:
            notes.append("no starter image available")

    # 3. Audio / Video inputs : upload latest output if available
    if "audio" in inputs_needed:
        latest_a = latest_output("audio")
        if latest_a:
            notes.append(f"audio input: latest output {latest_a}")
            for nid, node in api_graph.items():
                if "Audio" in node.get("class_type", "") and "Load" in node.get("class_type", ""):
                    node.setdefault("inputs", {})["audio"] = latest_a
        else:
            notes.append("no latest audio available — workflow may fail")
    if "video" in inputs_needed:
        latest_v = latest_output("video")
        if latest_v:
            notes.append(f"video input: latest output {latest_v}")
            for nid, node in api_graph.items():
                ct = node.get("class_type", "")
                if "Video" in ct and "Load" in ct:
                    node.setdefault("inputs", {})["video"] = latest_v
        else:
            notes.append("no latest video available — workflow may fail")

    return api_graph, notes


def test_workflow(client: ComfyClient, template_id: str, klass: dict,
                  prompt: str, timeout: int = 600) -> dict:
    """Run one workflow test, return result dict."""
    from comfyui_workflow_templates_core.loader import get_asset_path
    try:
        json_path = get_asset_path(template_id, template_id + ".json")
        wf = json.loads(Path(json_path).read_text(encoding="utf-8"))
    except Exception as e:
        return {"status": "error", "phase": klass.get("phase"),
                "error": f"load: {e}", "duration": 0}

    api_graph = workflow_ui_to_api(wf)
    if not api_graph:
        return {"status": "error", "phase": klass.get("phase"),
                "error": "could not convert UI→API format", "duration": 0}

    # Inject inputs intelligently based on category
    api_graph, notes = inject_workflow_inputs(client, api_graph, klass, prompt)

    # Submit
    t0 = time.time()
    try:
        prompt_id = client.queue_prompt(api_graph)
    except urllib.error.HTTPError as e:
        body = ""
        try: body = e.read().decode()
        except: pass
        return {"status": "error", "phase": klass.get("phase"),
                "error": f"HTTP {e.code}: {body[:300]}", "duration": time.time()-t0}
    except Exception as e:
        return {"status": "error", "phase": klass.get("phase"),
                "error": f"submit: {e}", "duration": time.time()-t0}

    # Poll history
    while time.time() - t0 < timeout:
        h = client.history(prompt_id)
        if prompt_id in h:
            entry = h[prompt_id]
            outputs = entry.get("outputs", {})
            status_obj = entry.get("status", {})
            if status_obj.get("status_str") == "success" or outputs:
                # Find output files
                output_files = []
                for _node_id, out in outputs.items():
                    for kind in ("images", "gifs", "audio", "videos"):
                        for f in (out.get(kind) or []):
                            if isinstance(f, dict) and f.get("filename"):
                                output_files.append({
                                    "kind": kind, "filename": f["filename"],
                                    "subfolder": f.get("subfolder", ""),
                                    "type": f.get("type", "output"),
                                })
                return {"status": "ok", "phase": klass.get("phase"),
                        "duration": time.time()-t0,
                        "outputs": output_files,
                        "prompt_id": prompt_id}
            # If status reports an error
            if status_obj.get("status_str") == "error":
                msgs = status_obj.get("messages", [])
                return {"status": "error", "phase": klass.get("phase"),
                        "error": "; ".join(str(m) for m in msgs)[:500],
                        "duration": time.time()-t0,
                        "prompt_id": prompt_id}
        time.sleep(2)

    return {"status": "timeout", "phase": klass.get("phase"),
            "duration": time.time()-t0, "prompt_id": prompt_id}


def cmd_test(args):
    client = ComfyClient(args.api)

    # Verify ComfyUI is up
    try:
        client._get_json("/queue", timeout=5)
    except Exception as e:
        print(f"ERREUR: ComfyUI inaccessible à {args.api}: {e}", file=sys.stderr)
        sys.exit(1)

    classes = load_classes()
    results = load_results()

    # Decide which workflows to run
    targets = []
    phases = list(range(1, 7)) if args.all else [args.phase]
    for phase in phases:
        if phase is None: continue
        for tid, k in classes.items():
            if k.get("phase") == phase:
                if args.skip_done and tid in results["by_workflow"] and \
                   results["by_workflow"][tid].get("status") == "ok":
                    continue
                targets.append((tid, k))

    if args.limit:
        targets = targets[:args.limit]

    if not targets:
        print("Aucun workflow à tester (déjà tous exécutés ou phase vide)")
        return

    print(f"🧪 Testing {len(targets)} workflows" +
          (f" (phases {phases})" if args.all else f" (phase {args.phase})"))
    print()

    run_id = datetime.now().isoformat()
    run_results = []

    for i, (tid, k) in enumerate(targets, 1):
        cat = k.get("category", "general")
        phase = k.get("phase", 0)
        print(f"  [{i}/{len(targets)}] {tid} (ph{phase}, {cat})...")

        # Choose prompt by phase / category :
        # - phase 1/2/3 : use a category-flavored generation prompt
        # - phase 4+ (edit) : use the EDIT_PROMPTS map for cohesion with starter
        if phase in (4, 5, 6, 7, 8, 9, 10):
            prompt = EDIT_PROMPTS.get(cat, EDIT_PROMPTS["general"])
        else:
            # Use the first default prompt of the workflow itself if available,
            # otherwise pick a category-flavored one.
            workflow_prompts = k.get("default_prompts") or []
            if workflow_prompts:
                # Use the first non-empty workflow prompt (preserves intent)
                prompt = next((p for p in workflow_prompts if len(p) >= 10), DEFAULT_PROMPTS[0])
            else:
                # Fall back to starter prompt from the category map
                from generate_starters import STARTER_PROMPTS
                prompt = STARTER_PROMPTS.get(cat, DEFAULT_PROMPTS[0])

        res = test_workflow(client, tid, k, prompt, timeout=args.timeout)
        if res.get("status") == "ok" or res.get("status") == "error":
            res["category"] = cat
            res["used_prompt"] = prompt[:100]

        # Update LATEST_OUTPUTS for chaining
        if res.get("status") == "ok":
            for f in res.get("outputs", []):
                kind = f["kind"].rstrip("s")  # 'images' → 'image', 'videos' → 'video'
                set_latest_output(kind, f["filename"])

        results["by_workflow"][tid] = {**res, "last_run": run_id}
        run_results.append({"tid": tid, **res})

        # Live save
        save_results(results)

        status_icon = {"ok": "✓", "error": "✗", "timeout": "⏱"}.get(res["status"], "?")
        line = f"      {status_icon} {res['status']:8} {fmt_dur(res['duration'])}"
        if res.get("error"):
            line += f"  err: {res['error'][:80]}"
        print(line)

        # Copy successful workflows
        if res["status"] == "ok":
            try:
                from comfyui_workflow_templates_core.loader import get_asset_path
                src = get_asset_path(tid, tid + ".json")
                dest = WORKING_DIR / f"{tid}.json"
                shutil.copy2(src, dest)
            except Exception:
                pass

    results["runs"].append({
        "run_id": run_id, "phase": args.phase, "all": args.all,
        "tested": len(run_results),
        "ok": sum(1 for r in run_results if r["status"] == "ok"),
        "error": sum(1 for r in run_results if r["status"] == "error"),
        "timeout": sum(1 for r in run_results if r["status"] == "timeout"),
    })
    save_results(results)

    print()
    print(f"📊 Résumé : {results['runs'][-1]['ok']} OK, "
          f"{results['runs'][-1]['error']} errors, "
          f"{results['runs'][-1]['timeout']} timeouts")
    print(f"   Workflows fonctionnels exportés : {WORKING_DIR}")


def cmd_report(args):
    if not RESULTS_FILE.exists():
        print("Aucun résultat. Lance d'abord `test`.")
        return
    results = load_results()
    by_status = {"ok": [], "error": [], "timeout": []}
    for tid, r in results["by_workflow"].items():
        by_status.setdefault(r["status"], []).append((tid, r))

    for status in ["ok", "error", "timeout"]:
        items = by_status.get(status, [])
        print(f"\n[{status.upper()}] {len(items)} workflow(s):")
        for tid, r in sorted(items)[:30]:
            line = f"  • {tid:55} ph{r.get('phase','?')}  {fmt_dur(r.get('duration', 0))}"
            if r.get("error"):
                line += f"  err: {r['error'][:50]}"
            print(line)
        if len(items) > 30:
            print(f"  ... +{len(items)-30}")


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--api", default="http://127.0.0.1:8188")
    sp = parser.add_subparsers(dest="cmd")
    sp.required = False  # default: test

    # Default command = test
    parser.add_argument("--phase", type=int, default=1)
    parser.add_argument("--all", action="store_true", help="Toutes phases 1→6")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--timeout", type=int, default=600,
                         help="Timeout par workflow (sec)")
    parser.add_argument("--skip-done", action="store_true",
                         help="Sauter les workflows déjà OK")
    parser.add_argument("--report", action="store_true", help="Voir le rapport")

    args = parser.parse_args()

    if args.report:
        cmd_report(args)
    else:
        cmd_test(args)


if __name__ == "__main__":
    main()
