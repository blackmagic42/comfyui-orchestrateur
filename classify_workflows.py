#!/usr/bin/env python
"""
Classifie chaque workflow ComfyUI selon ses I/O :
  - inputs requis : text prompt, image, video, audio
  - outputs produits : image, video, audio, mesh 3D, etc.
  - paramètres exposés : prompt, seed, steps, cfg, etc.

Génère un manifest `.catalog_state/workflow_classes.json` qui pilote le test
harness automatique.

Stratégie de phases d'exécution :
  Phase 1 — text→image  (zéro input, juste un prompt)
  Phase 2 — text→audio  (zéro input)
  Phase 3 — text→video  (zéro input, lourd)
  Phase 4 — image→image (utilise outputs phase 1)
  Phase 5 — image→video (utilise outputs phase 1)
  Phase 6 — video→video (utilise outputs phase 5)

Usage :
    python classify_workflows.py
    python classify_workflows.py --phase 1   # liste seulement la phase
"""
from __future__ import annotations
import sys
sys.stdout.reconfigure(encoding="utf-8")

import argparse
import json
from collections import Counter, defaultdict
import os
from pathlib import Path

STATE_DIR = Path(os.environ.get("COMFYUI_STATE_DIR",
    str(Path(__file__).resolve().parent.parent / ".catalog_state")))
STATE_DIR.mkdir(exist_ok=True)
OUTPUT_FILE = STATE_DIR / "workflow_classes.json"


# Heuristiques par type de node — élargies pour couvrir tous les loaders
INPUT_NODES = {
    "image": {
        "LoadImage", "LoadImageMask", "LoadImageBatch",
        "ETN_LoadImageBase64", "VHS_LoadImages",
    },
    "video": {
        "VHS_LoadVideo", "VHS_LoadVideoPath", "LoadVideo", "VideoFrameLoader",
        "VHS_LoadVideoFFmpegUpload", "ImageOnlyCheckpointLoader",
    },
    "audio": {
        "LoadAudio", "VHS_LoadAudio", "AudioLoader", "ACEStepLoadAudio",
        "Comfy.LoadAudio", "VHS_LoadAudioUpload",
    },
    "mesh3d": {"Hy3DLoadMesh", "LoadMesh"},
}

OUTPUT_NODES = {
    "image": {
        "SaveImage", "PreviewImage", "ETN_SendImageWebSocket",
        "Image Save", "ImageSave", "PreviewBridge",
    },
    "video": {
        "VHS_VideoCombine", "SaveVideo", "VHS_SaveVideo",
        "SaveAnimatedWEBP", "SaveAnimatedPNG", "ImageToVideo",
        "Video Combine", "VideoSave", "VHS_SaveVideoFFmpeg",
    },
    "audio": {
        "SaveAudio", "VHS_SaveAudio", "PreviewAudio",
        "ACEStepSaveAudio", "ACEStepGenerate", "ACEStep",
        "Audio Save", "AudioSaveSimple", "Comfy.SaveAudio",
    },
    "mesh3d": {"Hy3DExportMesh", "SaveMesh", "Save3DMesh"},
}

# Heuristique secondaire : détecter par préfixe/keyword sur le nom du node
def _has_node_keyword(types, *keywords):
    """Returns True if any node type contains any of the keywords (case-insensitive)."""
    return any(any(kw.lower() in t.lower() for kw in keywords) for t in types)

PROMPT_NODES = {
    "CLIPTextEncode", "CLIPTextEncodeSDXL", "T5TextEncode", "FluxGuidance",
    "ImpactWildcardProcessor", "smZ CLIPTextEncode",
}

SAMPLER_NODES = {"KSampler", "KSamplerAdvanced", "SamplerCustom",
                 "BasicGuider", "CFGGuider", "DPMSolverMultistepSampler"}


def extract_default_prompts(nodes: list) -> list[str]:
    """Walks CLIPTextEncode-like nodes and returns their default prompt strings."""
    out = []
    PROMPT_TYPES = {"CLIPTextEncode", "CLIPTextEncodeSDXL", "T5TextEncode",
                    "TextEncodeAceStepAudio1.5", "smZ CLIPTextEncode",
                    "FluxGuidance", "ImpactWildcardProcessor",
                    "PrimitiveStringMultiline", "PrimitiveString"}
    for n in nodes:
        if not isinstance(n, dict):
            continue
        t = n.get("type") or ""
        if t not in PROMPT_TYPES and "text" not in t.lower() and "prompt" not in t.lower():
            continue
        widgets = n.get("widgets_values") or []
        for w in widgets:
            if isinstance(w, str) and len(w) >= 5 and len(w) <= 1000:
                out.append(w.strip())
    return out


# Category keywords — matched on (template_id + default_prompts + node names)
CATEGORY_KEYWORDS = {
    "portrait":     ["portrait", "face", "headshot", "1girl", "1boy",
                     "character_portrait", "selfie", "headshot", "facereaktor",
                     "facedetailer", "instantid", "ipadapter_face", "portraitlight"],
    "scene":        ["landscape", "scene", "environment", "background",
                     "city", "forest", "mountain", "beach", "vista", "outdoor"],
    "object":       ["product", "object", "isolated", "studio_shot",
                     "product_shot", "merch", "still_life", "logo"],
    "anime":        ["anime", "manga", "1girl", "illustration", "style_anime",
                     "manga_panel", "shoujo", "comic"],
    "abstract":     ["abstract", "geometric", "pattern", "texture",
                     "style_transfer", "gummy", "psychedelic"],
    "controlnet_pose": ["openpose", "pose", "controlnet_pose", "skeleton",
                        "sdpose", "stickfigure"],
    "controlnet_depth": ["depth", "midas", "lotus_depth"],
    "controlnet_canny": ["canny", "lineart", "edges"],
    "inpaint":      ["inpaint", "outpaint", "fill", "mask"],
    "upscale":      ["upscale", "super_resolution", "esrgan", "2k",
                     "spatial_upscaler", "face_restore"],
    "relight":      ["relight", "lighting", "portrait_light", "shadow",
                     "lightcontrol"],
    "video_dance":  ["dance", "humo", "music_video"],
    "lipsync":      ["lipsync", "lip_sync", "talking_head", "infinitetalk",
                     "speech_to_video"],
    "music":        ["music", "song", "instrumental"],
    "voice":        ["voice", "speech", "tts", "voice_clone", "vocal"],
    "graphic":      ["poster", "graphic_design", "thumbnail", "typography"],
    "3d":           ["3d", "mesh", "hunyuan3d", "model_to_view"],
    "lora_train":   ["train", "training", "fluxtrainer"],
}


def detect_category(template_id: str, default_prompts: list[str], types: Counter) -> str:
    """Returns the most relevant category given template_id, prompts, and nodes."""
    blob = template_id.lower() + " " + " ".join(default_prompts).lower()
    blob += " " + " ".join(types.keys()).lower()

    # Score each category by keyword hits
    scores = {}
    for cat, keywords in CATEGORY_KEYWORDS.items():
        score = 0
        for kw in keywords:
            if kw in blob:
                score += blob.count(kw)
        if score > 0:
            scores[cat] = score

    if not scores:
        return "general"
    # Return highest-scoring category
    return max(scores, key=scores.get)


def extract_workflow_class(workflow: dict, template_id: str = "") -> dict:
    """Returns: {inputs:[], outputs:[], category, default_prompts, ...}"""
    if not isinstance(workflow, dict):
        return {"inputs": [], "outputs": [], "node_types": []}

    nodes = []
    if isinstance(workflow.get("nodes"), list):
        nodes.extend(workflow["nodes"])
    if isinstance(workflow.get("definitions"), dict):
        for sg in (workflow["definitions"].get("subgraphs") or []):
            if isinstance(sg, dict):
                nodes.extend(sg.get("nodes") or [])

    types = Counter()
    for n in nodes:
        if not isinstance(n, dict):
            continue
        t = n.get("type") or ""
        if t:
            types[t] += 1

    default_prompts = extract_default_prompts(nodes)
    category = detect_category(template_id, default_prompts, types)

    inputs = []
    outputs = []
    for kind, names in INPUT_NODES.items():
        if any(t in types for t in names):
            inputs.append(kind)
    for kind, names in OUTPUT_NODES.items():
        if any(t in types for t in names):
            outputs.append(kind)

    # Fallback heuristics: detect by keyword match in node type names
    # Audio: any node with 'audio' or 'AceStep' / 'ACE' in its name
    if "audio" not in outputs and _has_node_keyword(types, "saveaudio", "vaedecodeaudio",
                                                     "acestep", "ace_step", "audiosave",
                                                     "previewaudio"):
        outputs.append("audio")
    if "audio" not in inputs and _has_node_keyword(types, "loadaudio", "audioload",
                                                    "audioupload"):
        inputs.append("audio")
    # Video: catch additional video patterns
    if "video" not in outputs and _has_node_keyword(types, "videocombine", "savevideo",
                                                     "saveanimated"):
        outputs.append("video")
    if "video" not in inputs and _has_node_keyword(types, "loadvideo", "videoload"):
        inputs.append("video")

    prompt_count = sum(types.get(t, 0) for t in PROMPT_NODES)
    sampler_count = sum(types.get(t, 0) for t in SAMPLER_NODES)

    # Detect special node types (LoRAs, controlnet, etc.)
    has_lora = any("Lora" in t for t in types)
    has_controlnet = any("ControlNet" in t for t in types)
    has_ipadapter = any("IPAdapter" in t for t in types)

    return {
        "inputs": sorted(set(inputs)),
        "outputs": sorted(set(outputs)),
        "prompt_nodes": prompt_count,
        "sampler_nodes": sampler_count,
        "has_lora": has_lora,
        "has_controlnet": has_controlnet,
        "has_ipadapter": has_ipadapter,
        "node_count": len(nodes),
        "node_types": dict(types.most_common(10)),
        "category": category,
        "default_prompts": default_prompts[:5],  # top 5 prompts for context
    }


def assign_phase(klass: dict) -> int:
    """Assigns workflow to test phase 1-9.

    Single-input phases:
      Phase 1 — text→image  (no input, prompts only)
      Phase 2 — text→audio
      Phase 3 — text→video
      Phase 4 — image→image
      Phase 5 — image→video
      Phase 6 — video→video
      Phase 7 — audio→audio (mastering, voice clone, etc.)

    Multi-input phases (need outputs from earlier phases as inputs):
      Phase 8 — image+audio→video (lipsync, talking heads)
      Phase 9 — image+video→video (style transfer w/ guide)
      Phase 10 — video+audio→video (audio-driven video animation)
    """
    inputs = set(klass.get("inputs") or [])
    outputs = set(klass.get("outputs") or [])

    has_image_in = "image" in inputs
    has_video_in = "video" in inputs
    has_audio_in = "audio" in inputs
    has_image_out = "image" in outputs
    has_video_out = "video" in outputs
    has_audio_out = "audio" in outputs

    # Multi-input — priority over single-input
    if has_image_in and has_audio_in and has_video_out:
        return 8  # image+audio→video (lipsync/talking head)
    if has_video_in and has_audio_in and has_video_out:
        return 10  # video+audio→video
    if has_image_in and has_video_in and has_video_out:
        return 9  # image+video→video
    if has_image_in and has_audio_in:
        # Multi-input but unclear output — usually animation/slideshow
        return 8

    # Single-input pure text-conditioned
    if not inputs:
        if has_image_out: return 1
        if has_audio_out: return 2
        if has_video_out: return 3
        return 0

    # Single-input transforms
    if has_image_in and not has_video_in and not has_audio_in:
        if has_video_out: return 5
        if has_image_out: return 4
        return 0
    if has_video_in and not has_audio_in:
        if has_video_out: return 6
        return 0
    if has_audio_in and has_audio_out:
        return 7
    if has_audio_in and has_video_out:
        return 8  # audio-driven animation falls here too

    return 0  # uncategorized


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--phase", type=int, default=None,
                         help="Affiche uniquement les workflows de la phase donnée (1-10)")
    parser.add_argument("--show-prompts", action="store_true",
                         help="Affiche les prompts par défaut (avec --phase)")
    args = parser.parse_args()

    try:
        from comfyui_workflow_templates_core.loader import load_manifest, get_asset_path
    except ImportError:
        print("ERREUR: comfyui_workflow_templates_core non installé", file=sys.stderr)
        sys.exit(1)

    manifest = load_manifest()
    locals_only = [t for t in manifest.templates.values()
                   if t.bundle != "media-api" and not t.template_id.startswith("api_")]

    classes = {}
    for t in locals_only:
        try:
            json_path = get_asset_path(t.template_id, t.template_id + ".json")
            with open(json_path, encoding="utf-8") as f:
                wf = json.load(f)
        except Exception as e:
            classes[t.template_id] = {"error": str(e), "phase": 0}
            continue

        klass = extract_workflow_class(wf, template_id=t.template_id)
        klass["phase"] = assign_phase(klass)
        classes[t.template_id] = klass

    OUTPUT_FILE.write_text(json.dumps(classes, indent=2), encoding="utf-8")

    # Tally
    by_phase = defaultdict(list)
    for tid, k in classes.items():
        by_phase[k.get("phase", 0)].append(tid)

    PHASE_LABELS = {
        1: "text→image",
        2: "text→audio",
        3: "text→video",
        4: "image→image",
        5: "image→video",
        6: "video→video",
        7: "audio→audio",
        8: "img+audio→video",
        9: "img+video→video",
        10: "video+audio→video",
        0: "uncategorized",
    }

    print(f"📋 {len(classes)} workflows classifiés ({OUTPUT_FILE})")
    print()
    print(f"{'PHASE':6} {'TYPE':18} {'COUNT':>6}")
    print("-" * 40)
    for phase in [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 0]:
        items = by_phase.get(phase, [])
        if not items:
            continue
        label = PHASE_LABELS[phase]
        print(f"{phase:6} {label:18} {len(items):>6}")
    print("-" * 40)
    print(f"{'TOTAL':6} {'':18} {len(classes):>6}")

    if args.phase is not None:
        items = by_phase.get(args.phase, [])
        print()
        print(f"\n=== Phase {args.phase} ({PHASE_LABELS.get(args.phase, '?')}) — {len(items)} workflows ===\n")
        # Group by category for readability
        by_cat = {}
        for tid in items:
            by_cat.setdefault(classes[tid].get("category", "?"), []).append(tid)

        for cat in sorted(by_cat.keys()):
            print(f"\n  📂 {cat}  ({len(by_cat[cat])}):")
            for tid in sorted(by_cat[cat]):
                k = classes[tid]
                extras = []
                if k.get("has_lora"): extras.append("LoRA")
                if k.get("has_controlnet"): extras.append("ControlNet")
                if k.get("has_ipadapter"): extras.append("IPAdapter")
                extras_s = " [" + ",".join(extras) + "]" if extras else ""
                print(f"     • {tid}{extras_s}")
                if args.show_prompts and k.get("default_prompts"):
                    for p in k["default_prompts"][:2]:
                        if len(p) > 120:
                            p = p[:117] + "..."
                        print(f"        🗨 {p}")


if __name__ == "__main__":
    main()
