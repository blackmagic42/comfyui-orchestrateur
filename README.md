# ComfyUI Orchestrator

Install, manage, and load-balance fleets of [ComfyUI](https://github.com/comfyanonymous/ComfyUI) instances across one or many machines — locally, on a cluster, or behind a public VPS.

> **Why**: ComfyUI is great for one user on one machine. As soon as you want
> *N* instances on different disks, *N* GPUs spread across DGX boxes, or you
> need to receive jobs from a public VPS and route them to whichever
> ComfyUI is least busy, you need glue. This is that glue.

---

## What this gives you

```
                          ┌──────────────┐
   workflow API jobs  ──► │ Orchestrator │ ──┬──► ComfyUI :8188
   from your VPS          │   :9000      │   ├──► ComfyUI :8189
                          └──────┬───────┘   ├──► ComfyUI :8190
                                 │           └──► ComfyUI on dg2:8188 (NFS)
                          dashboard
                          status, queue, logs
```

- **Install** — one-shot bootstrap of ComfyUI + custom nodes + curated workflows on a fresh machine (Linux, Windows, macOS).
- **Manage** — interactive launcher detects what's installed, shows live status, lets you start / stop / open / view logs of every instance.
- **Cluster** — deploy ComfyUI on N machines that share models via NFS, or pool them with mergerfs (each host stores `1/N`, sees `N/N`).
- **Route jobs** — `orchestrator.py` accepts workflow-API JSON (the format ComfyUI's `/prompt` endpoint expects), picks a healthy instance, runs it, and returns the output. Round-robin load balancing built in.
- **Dashboard** — HTML status page on `:9000/dashboard` showing every instance, every queued job, every test run.

Everything is **stdlib Python + bash + PowerShell**. No `pip install` for the orchestrator itself, no Docker required, no external services.

---

## Quick start

### 1. On a single machine

```bash
git clone https://github.com/blackmagic42/comfyui-orchestrateur.git
cd comfyui-orchestrateur
python setup.py
```

The interactive menu walks you through:
- "Single instance" — installs ComfyUI to `~/comfyui` (Linux) or `C:\Users\…\comfyui` (Windows), launches the orchestrator on `:9000`.
- "Multi-instance" — N installs on N disks (e.g. `D:\ComfyUI-Flux`, `E:\ComfyUI-SD3`), each on its own port. Each gets a `start_instance.sh` / `.ps1` launcher.

### 2. On a cluster

```bash
python setup.py            # then pick option 3 (NFS shared) or 4 (pool mergerfs)
```

You'll be prompted for:
- the list of SSH hosts (`user@host`, one per line)
- which one is the "primary" (downloads models)
- the shared mount path or the pool config
- the budget in GB

The script delegates to `deploy_cluster.sh` to rsync code + run remote installs over SSH.

### 3. Re-run any time

```bash
python setup.py
```

If you've already installed something, you'll see it on the main menu:

```
📦 Instances enregistrées : 3

  1. ● running  Flux         D:/ComfyUI-Flux       :8188
  2. ○ stopped  SD3          E:/ComfyUI-SD3        :8189
  3. ☁ cluster  DGX-Pool     4 hosts · pool-mergerfs

Que veux-tu faire ?
  m) Gérer les instances ci-dessus
  1) Installer une nouvelle instance
  ...
```

`m` lets you start / stop / open the dashboard / view logs / remove from registry.

---

## VPS → ComfyUI flow

The whole point: **a public VPS receives workflow JSON from a user, sends it to your private GPU fleet, returns the result**.

```
User browser ──HTTPS──► VPS (Caddy/Nginx) ──http──► Orchestrator (your LAN, :9000)
                                                       │
                                                       ▼
                                        Pick least-busy ComfyUI instance
                                                       │
                                                       ▼
                                          POST /prompt to that instance
                                                       ▼
                                          Wait for image / video output
                                                       ▼
                                                 Return to VPS
```

Bring your own VPN/tunnel (Tailscale, Wireguard, ssh -R) so the VPS can reach the orchestrator without exposing it. See [`setup_tunnel.sh`](setup_tunnel.sh) for an example with a reverse SSH tunnel.

The orchestrator API speaks plain JSON-over-HTTP — anything that can `POST /api/run` with a workflow can use it.

---

## Installer

`setup.py` is the cross-platform entry point. The actual install logic lives in:

| File | What it does |
|---|---|
| `install_comfyui.sh` | Linux/macOS bootstrap: clone ComfyUI, install torch + CUDA, custom nodes, copy workflows, configure firewall, launch orchestrator. |
| `install_comfyui.ps1` | Same, for Windows native (PowerShell). |
| `setup.sh` | Bash menu wrapper (legacy — `setup.py` supersedes it). |

Custom nodes installed by default:
- [ComfyUI-Manager](https://github.com/Comfy-Org/ComfyUI-Manager)
- [comfyui-workflow-manager](https://github.com/blackmagic42/comfyui-workflow-manager) — the sister project for one-click model downloads from the sidebar.

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
python orchestrator.py serve --port 9000           # API + dashboard
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
| `setup.py` | **Cross-platform launcher + lifecycle manager** (start here) |
| `setup.sh` | Bash menu wrapper (legacy) |
| `install_comfyui.sh` / `.ps1` | Per-OS bootstrap |
| `deploy_cluster.sh` | Multi-host deployment over SSH |
| `orchestrator.py` | HTTP proxy + dashboard + load balancer |
| `comfyui_catalog.py` | Model catalog builder/downloader |
| `classify_workflows.py` | Classify workflows by I/O type |
| `test_workflows.py` | Run-and-verify harness |
| `check_gated_models.py` | HF gated-model probe |
| `export_workflows_api.py` | UI → API workflow converter |
| `generate_starters.py` | Synthesize starter inputs |
| `setup_tunnel.sh` | Reverse SSH tunnel example for VPS → LAN |
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
