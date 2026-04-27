#!/usr/bin/env bash
# watch_cluster.sh — Monitor live des téléchargements sur les hosts du cluster.
#
# Interroge l'API /workflow-manager/downloads de chaque host toutes les 3 secondes
# et affiche un résumé compact dans le terminal.
#
# Usage :
#   bash watch_cluster.sh                           # défaut: dg2 + dg4
#   bash watch_cluster.sh dg2@dg2 dg4@dg4 dg1@dg1   # liste custom

set -euo pipefail

HOSTS=("$@")
[[ ${#HOSTS[@]} -eq 0 ]] && HOSTS=("dg2@dg2" "dg4@dg4")

INTERVAL="${WATCH_INTERVAL:-3}"

trap 'echo; echo "Stopped."; exit 0' INT TERM

while true; do
    clear
    printf "\033[1;35m═══ ComfyUI Cluster Downloads — $(date +%H:%M:%S) ═══\033[0m\n"
    echo ""

    for h in "${HOSTS[@]}"; do
        host_only="${h##*@}"
        printf "\033[1;36m── %s ──\033[0m\n" "$h"
        ssh -o ConnectTimeout=2 -o BatchMode=yes "$h" \
            'curl -s --max-time 2 http://127.0.0.1:8188/workflow-manager/downloads 2>/dev/null' 2>/dev/null \
            | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
    downloads = d.get("downloads", [])
    if not downloads:
        print("  (rien en file)")
        sys.exit(0)
    by_status = {}
    for dl in downloads:
        by_status.setdefault(dl["status"], []).append(dl)
    for status in ["downloading", "pending", "complete", "error"]:
        items = by_status.get(status, [])
        if not items: continue
        icon = {"complete":"\033[32m✓\033[0m","downloading":"\033[34m▶\033[0m","error":"\033[31m✗\033[0m","pending":"\033[90m…\033[0m"}.get(status, "?")
        if status in ("downloading", "error"):
            for dl in items[:6]:
                err = " " + dl["error"][:40] if dl.get("error") else ""
                print(f"  {icon} {dl[\"filename\"][:55]:55} {dl[\"progress\"]:>3}%{err}")
            if len(items) > 6: print(f"  ... +{len(items)-6} {status}")
        else:
            print(f"  {icon} {len(items)} {status}")
except Exception as e:
    print(f"  (erreur: {e})")
' 2>/dev/null || echo "  (offline ou auth requis)"
        echo ""
    done
    echo "Refresh chaque ${INTERVAL}s · Ctrl+C pour quitter"
    sleep "$INTERVAL"
done
