# ComfyUI Orchestrator

Install, manage, and load-balance fleets of [ComfyUI](https://github.com/comfyanonymous/ComfyUI) instances on one machine or across a cluster.

> **Why**: ComfyUI is great for one user on one machine. As soon as you want
> *N* instances on different disks, or *N* GPUs spread across DGX boxes
> sharing the same model collection, you need glue. This is that glue.

---

## What this gives you

```
                          ┌──────────────┐
   workflow API jobs  ──► │ Orchestrator │ ──┬──► ComfyUI :8188
                          │   :9100      │   ├──► ComfyUI :8189
                          └──────┬───────┘   ├──► ComfyUI :8190
                                 │           └──► ComfyUI on dg2:8188 (NFS)
                          dashboard
                          status, queue, logs
```

- **Orchestrator-first** — `setup.py` installs *only* the orchestrator (a small stdlib-Python service). Everything else — installing ComfyUI, picking a model budget, deploying instances, managing the cluster — happens **in the dashboard**.
- **Web dashboard** at `http://127.0.0.1:9100/dashboard` :
  - "⚙ Install ComfyUI" button → bootstraps a fresh ComfyUI install
  - Budget slider (100 → 2000 GB, or presets 250/400/700/1500) with live preview of what fits
  - "✨ Apply changes" → rebuilds catalog, downloads new models, removes obsolete ones
  - Per-instance status (running, queue depth, last job)
- **Route jobs** — accepts ComfyUI workflow-API JSON, picks the least-loaded healthy instance, returns the output. Round-robin built in.
- **Cluster** — `deploy_cluster.sh` deploys to N hosts over SSH (NFS shared, or pool mergerfs where each host stores `1/N`, sees `N/N`).

Everything is **stdlib Python + bash + PowerShell**. No `pip install` for the orchestrator itself, no Docker required, no external services.

---

## Quick start

### 1. Install the orchestrator (the only manual step)

```bash
git clone https://github.com/blackmagic42/comfyui-orchestrateur.git
cd comfyui-orchestrateur
python setup.py --install
```

That's it. `setup.py` :
- creates `~/.comfyui-orchestrator/` for state (logs, pid, config)
- spawns `orchestrator.py serve --port 9100` as a detached background process
- opens `http://127.0.0.1:9100/dashboard` in your browser

### 2. From the dashboard, deploy ComfyUI

In the dashboard's **⚙ Commands** tab :
1. Click **⚙ Install ComfyUI** — fills install path + budget, hit Run
2. Watch the live log tail in the side panel
3. When done, your fresh ComfyUI is registered and visible in the **Instances** tab

### 3. Pick a model budget

In the same **Commands** tab, the bundle slider lets you pick how much disk
to allocate :

| Preset | GB | What you get |
|---|---|---|
| Minimal  | 250  | text→image essentials |
| Image    | 400  | Flux + Qwen-Edit + ControlNet |
| Standard | 700  | image + video latest |
| Full     | 1500 | everything, no cap |

Or drag the slider anywhere from 100 → 2000 GB. Preview shows exactly which
models fit and what gets dropped, **before** any download starts.

Click **✨ Apply changes** → build manifest → download missing → cleanup obsolete.

### 4. Re-run setup.py to manage the orchestrator

```bash
python setup.py
```

```
● Orchestrateur en cours · PID 12345 · port 9100
  Dashboard : http://127.0.0.1:9100/dashboard

Actions :
  o) Ouvrir le dashboard dans le navigateur
  s) Arrêter l'orchestrateur
  r) Redémarrer l'orchestrateur
  l) Voir le log
  t) Status détaillé
  q) Quitter
```

`m` lets you start / stop / open the dashboard / view logs / remove from registry.

---

## API

The orchestrator speaks plain JSON-over-HTTP — anything that can `POST /api/job` with a workflow ID + optional prompt overrides can use it. See [`orchestrator.py`](orchestrator.py) for the full whitelist of endpoints.

```
POST /api/job
  body: { "template_id": "flux_schnell", "prompt": "a parrot on a bicycle" }
  → orchestrator picks the least-loaded ComfyUI, forwards to its /prompt,
    polls /history, returns the resulting image / video / audio
```

---

## Installer

`setup.py` installs **only the orchestrator** — a small stdlib-Python service that runs in the background and serves the dashboard on `:9100`. Once it's up, the dashboard's **⚙ Install ComfyUI** button (which calls `bootstrap_install` in the whitelist) does the actual ComfyUI bootstrap by invoking :

| File | What it does | OS |
|---|---|---|
| `install_comfyui.sh` | Clone ComfyUI, create per-install venv, install torch CUDA, custom nodes, copy workflows, configure firewall. | Linux / macOS |
| `install_comfyui.ps1` | Same, for Windows native (PowerShell). Default cu130 stable, `-UseNightly` opt-in for cu132. | Windows |

Custom nodes installed by default:
- [ComfyUI-Manager](https://github.com/Comfy-Org/ComfyUI-Manager)
- [comfyui-workflow-manager](https://github.com/blackmagic42/comfyui-workflow-manager) — the sister project for one-click model downloads from the sidebar.

The user never has to call these scripts directly — the dashboard wraps them.

---

## Cluster

`deploy_cluster.sh` handles multi-host deployments. Two topologies:

### A. Shared NFS — 1 primary downloads, workers read

```
NAS / dg1 :  /srv/cluster_models   ◄── primary downloads here
                    │
                    ▼ NFS mount on every host at /mnt/cluster_models
        ┌────────┬──────┬──────┬──────┐
        ▼        ▼      ▼      ▼      ▼
       dg1      dg2    dg3    dg4    dg5
```

Use when: you have a NAS, low-latency LAN, and want a single source of truth for model files.

### B. Pool mode — every host stores 1/N, sees N/N via mergerfs

```
Host dg1 :  /data/local_shard   (~400 GB, exported via NFS)
            /mnt/peer_dg2..4    (NFS mounts of peers)
            ~/comfyui/models    (mergerfs union — 1.5 TB visible)
```

Use when: you have N disks of similar size and want the *catalog total*
to spread across them without duplication. Writes are routed to the local
shard, reads fan out across peers.

Full architecture in [`CLUSTER.md`](CLUSTER.md).

---

## Orchestrator (`orchestrator.py`)

A small HTTP service that:

- Discovers running ComfyUI instances on `127.0.0.1:8188-8192` (and other hosts you register).
- Auto-launches one if none is alive.
- Accepts `POST /api/run { "workflow": <json>, "client_id": "..." }`, picks an instance, forwards to `/prompt`, polls `/history`, returns the resulting media.
- Round-robin load balancing across healthy instances.
- Dashboard at `/dashboard` showing every instance, every workflow status, every test run.
- Persists state to `<repo>/.catalog_state/` (override via `$COMFYUI_STATE_DIR`).

```bash
python orchestrator.py serve --port 9100           # API + dashboard
python orchestrator.py status                      # list discovered instances
python orchestrator.py launch --port 8188          # spawn a ComfyUI instance
python orchestrator.py test --phase 1              # batch-run all phase-1 workflows
```

The dashboard ships in `dashboard/` — pure HTML + a tiny JS, no build step.

---

## Catalog & test harness

For curating a fleet's model collection and verifying workflows actually work:

| Script | Role |
|---|---|
| `comfyui_catalog.py` | Build a manifest of "current" models within a budget (≤ 1 TB by default), download via the ComfyUI workflow-manager extension. |
| `classify_workflows.py` | Sort the 218 reference workflows into 6 phases (text→image, image→video, …). |
| `test_workflows.py` | Run each workflow via `/prompt`, chain the output of phase N into phase N+1. |
| `check_gated_models.py` | Probe HuggingFace for gated models needing a token. |
| `export_workflows_api.py` | Convert UI-format workflows into API-format for the orchestrator. |
| `generate_starters.py` | Synthesize starter inputs (an image, a prompt) for workflows that need one. |

Re-run any of them — they're idempotent and resume from the persistent state directory.

---

## Files

| File | Role |
|---|---|
| `setup.py` | **Orchestrator installer + lifecycle** — start here |
| `orchestrator.py` | HTTP proxy + dashboard + load balancer (the long-running service) |
| `dashboard/` | Static HTML/JS served at `:9100/dashboard` |
| `install_comfyui.sh` / `.ps1` | Per-OS ComfyUI bootstrap (called by the dashboard, not by you) |
| `deploy_cluster.sh` | Multi-host deployment over SSH |
| `setup.sh` | Bash menu wrapper (legacy, predates the orchestrator-first model) |
| `comfyui_catalog.py` | Model catalog builder/downloader |
| `classify_workflows.py` | Classify workflows by I/O type |
| `test_workflows.py` | Run-and-verify harness |
| `check_gated_models.py` | HF gated-model probe |
| `export_workflows_api.py` | UI → API workflow converter |
| `generate_starters.py` | Synthesize starter inputs |
| `watch_cluster.sh` | Tail logs from N hosts simultaneously |
| `dashboard/` | Static HTML/JS for the dashboard |
| `CLUSTER.md` | Cluster architecture & gotchas |

---

## Environment variables

| Var | Default | Effect |
|---|---|---|
| `COMFYUI_STATE_DIR` | `<repo>/.catalog_state` | Where catalog manifest, test results, registry live |
| `COMFYUI_PATH` | `<repo>/../ComfyUI` | Where the local ComfyUI install lives |

Override either to fit your machine layout.

---

## Status

This is a personal-scale project that grew into something usable.
Issues and PRs welcome. License: Apache-2.0.

Sister project: [`comfyui-workflow-manager`](https://github.com/blackmagic42/comfyui-workflow-manager) — the sidebar custom_node that does the actual model downloads.
