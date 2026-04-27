#!/usr/bin/env python
"""
Génère un set d'images starter par catégorie via un workflow text→image rapide
(par défaut: flux_schnell, 4 steps).

Ces images servent d'inputs pour les workflows phase 4-5-6-8-9-10 qui ont
besoin d'une image de base. Plutôt que d'utiliser une image au hasard, le
test harness pioche une starter qui CORRESPOND à la catégorie du workflow
(portrait → starter portrait ; landscape → starter landscape ; etc.).

Stockage : .catalog_state/starters/<category>.png
"""
from __future__ import annotations
import sys
sys.stdout.reconfigure(encoding="utf-8")

import argparse
import json
import time
import os
from pathlib import Path

STATE_DIR = Path(os.environ.get("COMFYUI_STATE_DIR",
    str(Path(__file__).resolve().parent.parent / ".catalog_state")))
STARTERS_DIR = STATE_DIR / "starters"
STARTERS_DIR.mkdir(parents=True, exist_ok=True)

# Prompt par catégorie — formulé pour produire une image qui marche
# en input pour les workflows d'edit / chain de cette catégorie.
STARTER_PROMPTS = {
    "portrait":     "professional studio portrait of a young person, clear face, neutral lighting, photorealistic, 4k",
    "scene":        "wide angle landscape photograph, mountains and lake, golden hour, photorealistic, 4k",
    "object":       "isolated white background product shot of a coffee mug, professional studio lighting, photorealistic",
    "anime":        "anime style portrait of a character, clear features, vibrant colors, manga illustration",
    "abstract":     "abstract geometric composition, vibrant colors, modern art, 4k",
    "controlnet_pose": "full body portrait of a person standing, T-pose, neutral background, photorealistic",
    "controlnet_depth": "scene with clear depth — foreground tree, midground house, background mountains, photorealistic",
    "controlnet_canny": "minimalist line art subject, clear outline, simple background",
    "inpaint":      "portrait of a person with a clear background that can be replaced",
    "upscale":      "small detailed photograph of a flower, sharp focus",
    "relight":      "portrait of a person in even neutral lighting, photorealistic",
    "graphic":      "blank poster background, minimal design, white space",
    "lipsync":      "talking head portrait, mouth slightly open, neutral expression, photorealistic",
    "general":      "high quality photograph, sharp focus, photorealistic, 4k detail",
}

# Default text→image template to use as the starter generator
DEFAULT_GENERATOR_TEMPLATE = "flux_schnell"


def starter_path(category: str) -> Path:
    return STARTERS_DIR / f"{category}.png"


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--api", default="http://127.0.0.1:8188")
    parser.add_argument("--generator", default=DEFAULT_GENERATOR_TEMPLATE,
                         help="Template ID utilisé pour générer (défaut: flux_schnell)")
    parser.add_argument("--categories", nargs="*",
                         help="Catégories spécifiques (défaut: toutes)")
    parser.add_argument("--force", action="store_true",
                         help="Régénérer même si l'image starter existe déjà")
    parser.add_argument("--dry-run", action="store_true",
                         help="Affiche ce qui serait fait, sans appel API")
    args = parser.parse_args()

    cats = args.categories or list(STARTER_PROMPTS.keys())

    print(f"🌱 Génération starters dans {STARTERS_DIR}")
    print(f"   Generator template: {args.generator}")
    print(f"   Categories       : {len(cats)}")
    print()

    # Import test_workflows for the ComfyClient + workflow loader
    sys.path.insert(0, str(Path(__file__).parent))
    from test_workflows import ComfyClient, workflow_ui_to_api

    try:
        from comfyui_workflow_templates_core.loader import get_asset_path
        gen_path = get_asset_path(args.generator, args.generator + ".json")
        gen_workflow = json.loads(Path(gen_path).read_text(encoding="utf-8"))
    except Exception as e:
        print(f"ERREUR: impossible de charger le generator '{args.generator}': {e}",
              file=sys.stderr)
        sys.exit(1)

    client = ComfyClient(args.api)

    summary = {"created": [], "skipped": [], "errors": []}
    for cat in cats:
        prompt = STARTER_PROMPTS.get(cat)
        if not prompt:
            print(f"  ⚠ Pas de prompt défini pour '{cat}'")
            continue

        out_path = starter_path(cat)
        if out_path.exists() and not args.force:
            print(f"  ⊖ {cat}: déjà présent → {out_path.name}")
            summary["skipped"].append(cat)
            continue

        if args.dry_run:
            print(f"  [DRY] {cat}: prompt = '{prompt[:60]}...'")
            continue

        # Convert workflow to API format and inject the prompt
        api_graph = workflow_ui_to_api(gen_workflow)
        if not api_graph:
            print(f"  ✗ {cat}: conversion workflow échouée")
            summary["errors"].append(cat)
            continue

        # Find CLIPTextEncode node and override its prompt + inject random seed
        prompt_replaced = 0
        seed_replaced = 0
        import random as _random
        seed_value = _random.randint(0, 2**31 - 1)
        for node_id, node in api_graph.items():
            if node.get("class_type") in ("CLIPTextEncode", "CLIPTextEncodeSDXL", "T5TextEncode"):
                widgets = node.get("_meta", {}).get("_widgets", []) or []
                if widgets and isinstance(widgets[0], str):
                    # The text input is named "text"
                    node["inputs"]["text"] = prompt
                    prompt_replaced += 1
            if node.get("class_type") in ("KSampler", "KSamplerAdvanced"):
                node["inputs"]["seed"] = seed_value
                seed_replaced += 1

        if prompt_replaced == 0:
            print(f"  ⚠ {cat}: aucun CLIPTextEncode trouvé pour injecter le prompt")
            summary["errors"].append(cat)
            continue

        print(f"  → {cat}: génération via {args.generator}...")
        try:
            prompt_id = client.queue_prompt(api_graph)
        except Exception as e:
            print(f"     ✗ submit failed: {e}")
            summary["errors"].append(cat)
            continue

        # Poll
        t0 = time.time()
        timeout = 300  # 5 min max for a starter
        result = None
        while time.time() - t0 < timeout:
            h = client.history(prompt_id)
            if prompt_id in h:
                result = h[prompt_id]
                break
            time.sleep(2)

        if not result:
            print(f"     ✗ timeout")
            summary["errors"].append(cat)
            continue

        # Find the saved image
        outputs = result.get("outputs", {})
        found = None
        for _node_id, out in outputs.items():
            for img in (out.get("images") or []):
                if isinstance(img, dict) and img.get("filename"):
                    found = img
                    break
            if found:
                break

        if not found:
            print(f"     ✗ pas d'image dans le résultat")
            summary["errors"].append(cat)
            continue

        # Copy from output dir to our starters dir
        comfy_output = Path(os.environ.get("COMFYUI_PATH",
            str(Path(__file__).resolve().parent.parent / "ComfyUI"))) / "output"
        subfolder = found.get("subfolder", "")
        src = comfy_output / subfolder / found["filename"]
        if not src.exists():
            # Try via /view endpoint
            try:
                import urllib.parse
                import urllib.request
                params = urllib.parse.urlencode({
                    "filename": found["filename"],
                    "subfolder": subfolder,
                    "type": found.get("type", "output"),
                })
                with urllib.request.urlopen(f"{args.api}/view?{params}", timeout=30) as resp:
                    out_path.write_bytes(resp.read())
                print(f"     ✓ saved (via /view) → {out_path.name}")
                summary["created"].append(cat)
            except Exception as e:
                print(f"     ✗ download failed: {e}")
                summary["errors"].append(cat)
            continue

        import shutil
        shutil.copy2(src, out_path)
        print(f"     ✓ saved → {out_path.name} ({out_path.stat().st_size / 1024:.0f} KB)")
        summary["created"].append(cat)

    print()
    print(f"📊 {len(summary['created'])} créés · {len(summary['skipped'])} skip · "
          f"{len(summary['errors'])} erreurs")
    if summary["errors"]:
        print(f"   Erreurs : {summary['errors']}")


if __name__ == "__main__":
    main()
