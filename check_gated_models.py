#!/usr/bin/env python
"""
Pré-vol des modèles HuggingFace qui nécessitent acceptation de licence.

Pour chaque URL HF du download_list :
  - Tente HEAD sans token → si 401/403 → modèle "gated"
  - Avec token (env HF_TOKEN) → vérifie si l'accès est OK

Sortie :
  .catalog_state/gated_models.json   { gated: [...], accessible: [...] }
  .catalog_state/gated_models.md     guide utilisateur pour accepter

Le rapport markdown liste pour chaque modèle gated :
  - L'URL de la page du modèle (où accepter la licence)
  - L'URL du fichier (pour vérification)
  - L'instruction "Crée un token sur huggingface.co/settings/tokens"

Usage :
    HF_TOKEN=hf_xxx python check_gated_models.py
    python check_gated_models.py --no-token  # Voir ce qui est gated sans token
"""
from __future__ import annotations
import sys
sys.stdout.reconfigure(encoding="utf-8")

import argparse
import json
import os
import re
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

STATE_DIR = Path(os.environ.get("COMFYUI_STATE_DIR",
    str(Path(__file__).resolve().parent.parent / ".catalog_state")))
DOWNLOAD_LIST = STATE_DIR / "download_list.json"
OUT_JSON = STATE_DIR / "gated_models.json"
OUT_MD = STATE_DIR / "gated_models.md"


def hf_repo_from_url(url: str) -> str | None:
    m = re.match(r"https?://(?:huggingface\.co|hf\.co)/([^/]+/[^/]+)/(?:resolve|raw|blob)/", url)
    return m.group(1) if m else None


def hf_model_page(url: str) -> str | None:
    """https://huggingface.co/USER/REPO/resolve/main/file → https://huggingface.co/USER/REPO"""
    repo = hf_repo_from_url(url)
    return f"https://huggingface.co/{repo}" if repo else None


def head_check(url: str, token: str | None = None, timeout: int = 15) -> dict:
    headers = {"User-Agent": "comfyui-catalog/1.0"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        req = urllib.request.Request(url, method="HEAD", headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return {"status": resp.status, "ok": True}
    except urllib.error.HTTPError as e:
        return {
            "status": e.code,
            "ok": False,
            "reason": e.reason,
            "is_gated": e.code in (401, 403),
        }
    except Exception as e:
        return {"status": 0, "ok": False, "reason": str(e), "is_gated": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--no-token", action="store_true",
                         help="Ne pas utiliser HF_TOKEN même s'il est défini")
    parser.add_argument("--token", default=None,
                         help="Token HF explicite (sinon $HF_TOKEN)")
    args = parser.parse_args()

    if not DOWNLOAD_LIST.exists():
        print("download_list.json absent — lance `comfyui_catalog.py build` d'abord",
              file=sys.stderr)
        sys.exit(1)

    items = json.loads(DOWNLOAD_LIST.read_text(encoding="utf-8"))
    hf_items = [i for i in items if hf_repo_from_url(i.get("url", ""))]

    print(f"🔐 Vérification gated check sur {len(hf_items)} URLs HuggingFace")
    token = None if args.no_token else (args.token or os.environ.get("HF_TOKEN"))
    print(f"   Token : {'présent' if token else 'absent'}")
    print()

    accessible = []
    gated = []
    errors = []

    def check_one(item):
        return item, head_check(item["url"], token=token)

    with ThreadPoolExecutor(max_workers=15) as pool:
        futs = [pool.submit(check_one, i) for i in hf_items]
        done = 0
        for fut in as_completed(futs):
            item, res = fut.result()
            done += 1
            if done % 25 == 0:
                print(f"   {done}/{len(hf_items)}")

            if res["ok"]:
                accessible.append(item)
            elif res.get("is_gated"):
                gated.append({
                    **item,
                    "model_page": hf_model_page(item["url"]),
                    "status_code": res["status"],
                    "reason": res.get("reason", ""),
                })
            else:
                errors.append({**item, "status_code": res["status"], "reason": res.get("reason", "")})

    out = {
        "with_token": bool(token),
        "accessible": accessible,
        "gated": gated,
        "errors": errors,
        "summary": {
            "total": len(hf_items),
            "accessible": len(accessible),
            "gated": len(gated),
            "errors": len(errors),
        },
    }
    OUT_JSON.write_text(json.dumps(out, indent=2), encoding="utf-8")

    # Markdown report grouping gated models by repo
    md = []
    md.append("# Modèles HuggingFace gated — guide d'accès")
    md.append("")
    md.append(f"Token utilisé : {'oui' if token else 'non'}")
    md.append("")
    md.append(f"## Résumé")
    md.append(f"- ✅ Accessibles : **{len(accessible)}**")
    md.append(f"- 🔒 Gated (besoin licence/auth) : **{len(gated)}**")
    md.append(f"- ❌ Erreurs autres : **{len(errors)}**")
    md.append("")
    if gated:
        md.append("## 🔒 Modèles à débloquer")
        md.append("")
        md.append("Pour chaque modèle ci-dessous :")
        md.append("1. Ouvre la page du repo → clique sur **Agree to terms**")
        md.append("2. Crée un token (`read`) sur https://huggingface.co/settings/tokens")
        md.append("3. Exporte le token : `export HF_TOKEN=hf_xxx`")
        md.append("4. Re-lance ce check : `python check_gated_models.py`")
        md.append("")
        md.append("| Modèle | Code | Page (accept terms) |")
        md.append("|---|---|---|")
        # Group by repo to dedupe
        by_repo = {}
        for g in gated:
            repo = hf_repo_from_url(g["url"]) or "?"
            by_repo.setdefault(repo, []).append(g)
        for repo, items_in_repo in sorted(by_repo.items()):
            sample = items_in_repo[0]
            files_count = f"{len(items_in_repo)} files"
            md.append(f"| `{repo}` ({files_count}) | {sample['status_code']} | "
                      f"[{sample['model_page']}]({sample['model_page']}) |")
    if errors:
        md.append("")
        md.append("## ❌ Erreurs (autres que gated)")
        md.append("")
        for e in errors[:10]:
            md.append(f"- `{e['name']}` → HTTP {e['status_code']}: {e.get('reason','')}")

    OUT_MD.write_text("\n".join(md), encoding="utf-8")

    print()
    print(f"✅ Accessibles : {len(accessible)}")
    print(f"🔒 Gated       : {len(gated)}")
    print(f"❌ Erreurs     : {len(errors)}")
    print(f"\nRapport : {OUT_MD}")
    if gated and not token:
        print("\n💡 Pour débloquer ces modèles : suis les instructions dans le markdown.")


if __name__ == "__main__":
    main()
