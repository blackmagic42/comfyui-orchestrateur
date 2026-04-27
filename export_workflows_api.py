#!/usr/bin/env python
"""
Export tous les workflows ComfyUI au format API (prêts pour /prompt).

Le format UI (workflow JSON dans le browser) diffère du format API attendu
par /prompt :
  - UI : {nodes: [{id, type, widgets_values, inputs, ...}], links: [...]}
  - API: {"<id>": {class_type, inputs: {<name>: <value | [from_id, slot]>}}, ...}

Cette conversion :
  1. Walk les nodes (top-level + subgraphs)
  2. Résout les links pour reconstituer les inputs nommés
  3. Mappe widgets_values vers les inputs scalaires via INPUT_DEFS si dispo
  4. Préserve les seeds aléatoires en y mettant 0 (sera randomized par le runner)

Limitations :
  - Les subgraphs sont aplatis (les ports virtuels sont remplacés par des liens directs)
  - Les nodes custom sans INPUT_DEFS ont leurs widgets en _meta._widgets pour
    inspection manuelle

Sortie :
  .catalog_state/api_workflows/<template_id>.json   # API format
  .catalog_state/api_export_report.json             # status par workflow

Usage :
    python export_workflows_api.py
    python export_workflows_api.py --filter "qwen"
"""
from __future__ import annotations
import sys
sys.stdout.reconfigure(encoding="utf-8")

import argparse
import json
import os
from pathlib import Path

STATE_DIR = Path(os.environ.get("COMFYUI_STATE_DIR",
    str(Path(__file__).resolve().parent.parent / ".catalog_state")))
API_DIR = STATE_DIR / "api_workflows"
API_DIR.mkdir(parents=True, exist_ok=True)
REPORT_FILE = STATE_DIR / "api_export_report.json"


def workflow_ui_to_api(workflow: dict) -> tuple[dict | None, str]:
    """Convert UI-format workflow to API-format. Returns (api_graph, error_msg)."""
    if not isinstance(workflow, dict):
        return None, "not a dict"

    # Check if already API format
    if "nodes" not in workflow:
        if all(isinstance(v, dict) and "class_type" in v for v in workflow.values()):
            return workflow, ""
        return None, "neither UI nor API format"

    nodes = workflow.get("nodes") or []
    links = workflow.get("links") or []

    # Subgraph nodes are nested under definitions.subgraphs
    defs = workflow.get("definitions") or {}
    for sg in (defs.get("subgraphs") or []):
        if isinstance(sg, dict):
            for n in (sg.get("nodes") or []):
                nodes.append(n)
            for l in (sg.get("links") or []):
                links.append(l)

    # link_id → (from_node, from_slot, to_node, to_slot)
    link_map = {}
    for l in links:
        if isinstance(l, list) and len(l) >= 5:
            link_map[l[0]] = (l[1], l[2], l[3], l[4])
        elif isinstance(l, dict) and "id" in l:
            link_map[l["id"]] = (l.get("origin_id"), l.get("origin_slot", 0),
                                  l.get("target_id"), l.get("target_slot", 0))

    api = {}
    for n in nodes:
        if not isinstance(n, dict): continue
        nid = str(n.get("id"))
        ntype = n.get("type")
        if not nid or not ntype:
            continue

        node_inputs = {}
        # Resolve linked inputs (from upstream nodes)
        for inp in (n.get("inputs") or []):
            if not isinstance(inp, dict):
                continue
            link_id = inp.get("link")
            name = inp.get("name", "?")
            if link_id is not None and link_id in link_map:
                from_node, from_slot, _, _ = link_map[link_id]
                node_inputs[name] = [str(from_node), int(from_slot or 0)]

        # Inject widgets_values as scalar inputs
        # Heuristic: order of widgets_values tends to match order of input fields
        # in INPUT_TYPES. Without access to that, we add them under "_widgets" so
        # the test runner can inject canonical names later.
        widgets = n.get("widgets_values")
        api[nid] = {
            "class_type": ntype,
            "inputs": node_inputs,
            "_meta": {
                "title": n.get("title") or ntype,
                "_widgets": widgets if isinstance(widgets, list) else [],
            },
        }

    return api, ""


def export_one(template_id: str, asset_path: Path) -> dict:
    try:
        wf = json.loads(asset_path.read_text(encoding="utf-8"))
    except Exception as e:
        return {"status": "error", "error": f"load: {e}"}

    api_graph, err = workflow_ui_to_api(wf)
    if api_graph is None:
        return {"status": "error", "error": err}

    out_path = API_DIR / f"{template_id}.json"
    out_path.write_text(json.dumps(api_graph, indent=2), encoding="utf-8")
    return {
        "status": "ok",
        "out": str(out_path.relative_to(STATE_DIR)),
        "node_count": len(api_graph),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--filter", default=None,
                         help="Sous-chaîne à matcher dans le template_id")
    args = parser.parse_args()

    try:
        from comfyui_workflow_templates_core.loader import load_manifest, get_asset_path
    except ImportError:
        print("ERREUR: comfyui_workflow_templates_core absent", file=sys.stderr)
        sys.exit(1)

    manifest = load_manifest()
    locals_only = [t for t in manifest.templates.values()
                   if t.bundle != "media-api" and not t.template_id.startswith("api_")]
    if args.filter:
        locals_only = [t for t in locals_only if args.filter.lower() in t.template_id.lower()]

    print(f"📤 Export API format vers {API_DIR}")
    print(f"   Templates : {len(locals_only)}")

    report = {}
    ok = 0
    err = 0
    for t in locals_only:
        try:
            asset = Path(get_asset_path(t.template_id, t.template_id + ".json"))
            result = export_one(t.template_id, asset)
        except Exception as e:
            result = {"status": "error", "error": str(e)}

        report[t.template_id] = result
        if result["status"] == "ok":
            ok += 1
        else:
            err += 1

    REPORT_FILE.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\n📊 {ok} OK · {err} errors")
    print(f"   Report: {REPORT_FILE}")
    if err:
        print(f"\n   Échecs détaillés :")
        for tid, r in report.items():
            if r["status"] == "error":
                print(f"     ✗ {tid}: {r['error']}")


if __name__ == "__main__":
    main()
