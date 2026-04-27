# ComfyUI Cluster — déploiement multi-machines

Pour déployer ComfyUI sur **N machines** d'un même réseau (typiquement DGX) avec
les modèles **partagés** sur un volume réseau (NFS / SMB).

## 🏗 Architecture cible

```
                ┌──────────────────────────────────────────────┐
                │  NAS / serveur NFS                           │
                │  /srv/cluster_models/                        │
                │    checkpoints/  loras/  vae/  text_encoders/│
                │    ...                                       │
                └──────────────────────────────────────────────┘
                              ▲
                              │ NFS mount à /mnt/cluster_models
            ┌──────────────┬──┴──────────┬──────────────┐
            │              │             │              │
        ┌───┴────┐    ┌────┴────┐    ┌───┴────┐    ┌────┴───┐
        │  dg1   │    │   dg2   │    │  dg3   │    │  dg4   │
        │ PRIMARY│    │  worker │    │ worker │    │ worker │
        │ ↓dl    │    │ R-only  │    │ R-only │    │ R-only │
        │ orch   │    │ orch    │    │ orch   │    │ orch   │
        │ comfy  │    │ comfy   │    │ comfy  │    │ comfy  │
        └────────┘    └─────────┘    └────────┘    └────────┘
            :9000         :9000         :9000          :9000
            :8188         :8188         :8188          :8188
```

- **`dg1`** est désigné `primary` → c'est lui qui télécharge les modèles
- **`dg2-4`** sont des workers → ils utilisent les fichiers déjà sur le mount
- Tous ont leur propre orchestrateur (port 9000) et leur propre ComfyUI (port 8188)
- Les modèles vivent sur le NAS, accessibles via le même path `/mnt/cluster_models` sur chaque host
- Pendant un download, le mécanisme `.partial` du workflow-manager sur dg1 protège les workers (ils ne voient le fichier qu'après rename atomique)

## 📋 Pré-requis

Sur ton **laptop / poste de contrôle** :
- `bash`, `ssh`, `rsync`
- Clés SSH dans `~/.ssh/config` ou `authorized_keys` chez chaque DGX

Sur **chaque DGX** (dg1-4) :
- `python` 3.10+, `git`, `nvidia-smi`
- `~/.ssh/authorized_keys` contenant ta clé
- NFS client installé (`apt install nfs-common` sur Ubuntu)

Sur le **NAS** (ou un des DGX qui sert de serveur) :
- Disque dispo (≥ 1 TB recommandé pour catalog complet)
- NFS server installé (`apt install nfs-kernel-server`)

## 🔧 Setup NFS (5 minutes)

### Sur le serveur NFS (ex: dg1 ou un NAS dédié)

```bash
sudo apt install nfs-kernel-server -y
sudo mkdir -p /srv/cluster_models
sudo chown $USER:$USER /srv/cluster_models

# Export pour le subnet local (ex: 192.168.1.0/24)
echo "/srv/cluster_models 192.168.1.0/24(rw,sync,no_subtree_check,no_root_squash)" | sudo tee -a /etc/exports
sudo exportfs -ra
sudo systemctl enable --now nfs-kernel-server

# Vérifie que c'est exporté
sudo exportfs -v
```

### Sur chaque client (dg1-4)

```bash
sudo apt install nfs-common -y
sudo mkdir -p /mnt/cluster_models
# Si dg1 est aussi le serveur NFS, dg1 monte localement aussi pour avoir le même path
sudo mount -t nfs <NAS_IP>:/srv/cluster_models /mnt/cluster_models

# Pour un mount permanent au boot, dans /etc/fstab :
echo "<NAS_IP>:/srv/cluster_models /mnt/cluster_models nfs defaults,_netdev 0 0" | sudo tee -a /etc/fstab
```

Vérifie que le mount marche :

```bash
ssh dg1@dg1 'ls /mnt/cluster_models/'
ssh dg2@dg2 'ls /mnt/cluster_models/'
ssh dg3@dg3 'ls /mnt/cluster_models/'
ssh dg4@dg4 'ls /mnt/cluster_models/'
# Tous doivent voir les mêmes dossiers
```

## 🚀 Déploiement

Depuis ton poste de contrôle :

```bash
bash scripts/deploy_cluster.sh \
    --hosts "dg1@dg1 dg2@dg2 dg3@dg3 dg4@dg4" \
    --primary "dg1@dg1" \
    --shared-models /mnt/cluster_models \
    --budget 1500
```

Ce que ça fait pour **chaque host** :
1. `rsync` du dossier `scripts/` vers `~/.comfyui-scripts/`
2. Copie de `install_comfyui.sh` à la racine du `$HOME` distant
3. Lance `bash ~/install_comfyui.sh --shared-models /mnt/cluster_models [--primary]` sur le host
4. Récupère le code de retour

Le script ajoute `--primary` à `dg1` uniquement (le worker des autres skip le téléchargement).

### Mode parallèle

Pour gagner du temps, ajoute `--parallel` :

```bash
bash scripts/deploy_cluster.sh \
    --hosts "dg1@dg1 dg2@dg2 dg3@dg3 dg4@dg4" \
    --primary "dg1@dg1" \
    --shared-models /mnt/cluster_models \
    --parallel
```

Les 4 installs partent en même temps. Bon pour les pré-requis et torch (chacun
clone et installe en local), mais le download des modèles reste sur le primary uniquement.

### Mode dry-run

Vérifie ce que le script ferait sans rien faire :

```bash
bash scripts/deploy_cluster.sh --hosts "..." --dry-run
```

## 🌐 Mode pool — chaque DG stocke 1/N, voit N/N (catalogue complet partagé)

**Cas d'usage** : 4 DGX × 1 TB de disque → tu veux **tout** le catalogue (1.5 TB)
sans avoir à le dupliquer 4 fois. Chaque DG ne stocke que 1/4 (~400 GB)
mais voit l'intégralité via mergerfs/NFS.

```bash
bash scripts/deploy_cluster.sh setup-pool \
    --hosts "dg1@dg1 dg2@dg2 dg3@dg3 dg4@dg4"
```

### Architecture pool

```
Chaque DGX :
┌─────────────────────────────────────────────────┐
│ /data/comfyui_local_shard/   ← son shard        │
│   checkpoints/                                  │
│   loras/                                        │
│   ...                                           │
│       (~400 GB local, exporté via NFS)          │
│                                                 │
│ /mnt/peer_dg0/    ← shard de dg1 monté en NFS   │
│ /mnt/peer_dg1/    ← shard de dg2                │
│ /mnt/peer_dg2/    ← shard de dg3                │
│ /mnt/peer_dg3/    ← shard de dg4                │
│       (sauf le sien)                            │
│                                                 │
│ ~/comfyui/models/  ← UNION mergerfs (1.5 TB vu) │
│       (writes routés vers local_shard,           │
│        reads union sur tout)                    │
└─────────────────────────────────────────────────┘
```

### Ce que `setup-pool` fait sur chaque DG (1 commande)

1. **NFS export** de `/data/comfyui_local_shard` (avec `fsid=N` unique par host)
2. **NFS mount** des $((N-1))$ peers à `/mnt/peer_dg<idx>/`
3. **mergerfs mount** de `~/comfyui/models/` → union des branches local + peers
4. **Persiste dans `/etc/fstab`** pour boot
5. **Fallback automatique** sur symlink farm si mergerfs n'est pas dispo

```bash
# Variables d'environnement supportées
POOL_LOCAL_DIR=/data/comfyui_local_shard \
POOL_PEER_MOUNT=/mnt/peer_dg \
POOL_SUBNET=192.168.1.0/24 \
POOL_MODE=mergerfs \
bash scripts/deploy_cluster.sh setup-pool --hosts "..."
```

### Workflow complet pool mode

```bash
# 1. Install ComfyUI sur les 4 hosts (sans --shared-models, on veut local par défaut)
bash scripts/deploy_cluster.sh \
    --hosts "dg1@dg1 dg2@dg2 dg3@dg3 dg4@dg4" \
    --parallel

# 2. Setup le pool (NFS + mergerfs)
bash scripts/deploy_cluster.sh setup-pool \
    --hosts "dg1@dg1 dg2@dg2 dg3@dg3 dg4@dg4"

# 3. Sur n'importe quel DG (typiquement dg1 via dashboard) :
#    - Bundle Full (1500 GB) → ✨ Apply changes (juste le build, pas le download)
ssh dg1@dg1 'python ~/.comfyui-scripts/comfyui_catalog.py build --budget 1500'

# 4. Distribute le download sur les 4 (chaque DG prend 400 GB)
bash scripts/deploy_cluster.sh parallel-download \
    --hosts "dg1@dg1 dg2@dg2 dg3@dg3 dg4@dg4" \
    --primary "dg1@dg1"

# 5. Vérifie l'état
bash scripts/deploy_cluster.sh pool-status \
    --hosts "dg1@dg1 dg2@dg2 dg3@dg3 dg4@dg4"
```

Sortie de `pool-status` :
```
HOST                 LOCAL_SHARD     MODELS_VIEW     FILES
──────────────────────────────────────────────────────────────────
dg1@dg1              387G            1.4T            136
dg2@dg2              374G            1.4T            136
dg3@dg3              391G            1.4T            136
dg4@dg4              369G            1.4T            136
```

Chaque DG voit **1.4 TB** de modèles (le total) mais n'occupe **~380 GB** sur disque local.

### mergerfs vs symlink farm

| Critère | mergerfs | symlink farm |
|---|---|---|
| Install | `apt install mergerfs` | aucune |
| Write (downloader) | ✓ routé vers local_shard | ✗ casse les symlinks |
| Read | union, transparent | OK |
| ComfyUI compat | ✓ parfait | ✓ avec gotchas |
| Kernel features | FUSE | aucun |
| **Recommandation** | ✓ par défaut | fallback si FUSE indispo |

Le script choisit automatiquement mergerfs si dispo, sinon fallback symlink avec un avertissement.

## ⚡ Mode `parallel-download` — accélération du téléchargement

Au lieu que **dg1 télécharge seul** les 700 GB de modèles, tu peux faire en sorte
que **les 4 hosts téléchargent en parallèle** chacun 1/4 du catalogue.

```bash
# Étape 1 : sur dg1 (primary), build le manifest comme d'habitude
ssh dg1@dg1 'bash ~/install_comfyui.sh --shared-models /mnt/cluster_models --primary'
# (puis depuis le dashboard : Bundle → ✨ Apply changes (juste le build))

# Étape 2 : depuis ton poste de contrôle, lance le download parallèle
bash scripts/deploy_cluster.sh parallel-download \
    --hosts "dg1@dg1 dg2@dg2 dg3@dg3 dg4@dg4" \
    --primary "dg1@dg1" \
    --shared-models /mnt/cluster_models
```

### Algorithme de partition (équilibré par taille)

```
1. Tri tous les modèles par taille décroissante
2. Distribue round-robin :
     index 0  → worker 0  (le plus gros)
     index 1  → worker 1
     index 2  → worker 2
     index 3  → worker 3
     index 4  → worker 0  (2ᵉ plus gros)
     index 5  → worker 1
     ...
3. Chaque worker download les items où `index % total == shard`
```

→ Résultat : chaque worker prend **environ la même quantité de bytes**, peu importe
le nombre de fichiers qu'il prend. Pour 4 workers et 115 modèles à 700 GB : ~175 GB par worker.

### Gain attendu

Selon ton uplink :
- **NAS 1 Gbps** : avec 1 worker → ~110 MB/s, **~1h45 pour 700 GB**
- **NAS 10 Gbps + 4 workers** : 4 × 110 MB/s = ~440 MB/s, **~27 min**

⚠ Le bottleneck devient **HuggingFace** (~50-200 MB/s par connexion) et le **réseau du NAS**.

### Sécurité du shard parallèle

- Chaque worker écrit sur le NFS via le **mécanisme `.partial`** du workflow-manager
- Aucun risque de collision : les shards sont **disjoints** (modulo total = unique mapping)
- Le rename atomique POSIX `os.replace` est supporté nativement par NFSv4
- Si un worker plante en cours, son shard est **incomplet sur le NFS** mais le `.partial` reste invisible aux autres ComfyUI

### Reprise après interruption

Re-lance simplement :
```bash
bash scripts/deploy_cluster.sh parallel-download --hosts "..." --primary "..."
```

Chaque worker skippe les fichiers déjà présents sur le NFS (vérification taille exacte). Donc redémarrer une fois suffit pour rattraper les fichiers manqués.

## 🎛 Utilisation post-deploy

Tu as 4 dashboards :
- http://dg1:9000/dashboard  (primary)
- http://dg2:9000/dashboard
- http://dg3:9000/dashboard
- http://dg4:9000/dashboard

**Sur le primary (dg1)** :
1. Onglet ⚙ Commands → 📦 Bundle Standard (700 GB)
2. Click ✨ Apply changes → download des modèles dans `/mnt/cluster_models`
3. Pendant le download, les workers attendent (les fichiers `.partial` sont
   masqués des combos ComfyUI)

**Sur chaque worker (dg2-4)** :
- Le ComfyUI démarre → voit `/mnt/cluster_models` rempli au fur et à mesure
- Aucun téléchargement local
- Soumets des jobs : ils tournent sur la GPU locale, mais piochent les modèles via NFS

## 🔄 Multi-instance load balancer

Pour utiliser le cluster comme un seul backend depuis ton VPS :

```nginx
upstream comfyui_cluster {
    least_conn;
    server dg1:8188;
    server dg2:8188;
    server dg3:8188;
    server dg4:8188;
}

server {
    listen 443 ssl;
    server_name comfyui.example.com;

    location / {
        proxy_pass http://comfyui_cluster;
        proxy_set_header Host $host;
        proxy_read_timeout 600s;
    }
}
```

Ou utilise l'orchestrateur de **dg1** comme point d'entrée et lui ajoutes les
URLs des autres dans `discover_instances` (déjà supporté : il scanne 8188-8192
sur 127.0.0.1 ; étends à `dg2:8188` etc. par config).

## 🧹 Maintenance

### Resync les scripts après une modif locale

```bash
bash scripts/deploy_cluster.sh \
    --hosts "dg1@dg1 dg2@dg2 dg3@dg3 dg4@dg4" \
    --primary "dg1@dg1" \
    --shared-models /mnt/cluster_models \
    --parallel
```

L'install est idempotent : pas de re-clone, juste pull + dépendances + relance orchestrateur.

### Mettre à jour les modèles

Sur le dashboard de **dg1** :
- ⚙ Commands → 🔄 Sync (fetch + rebuild) → met à jour les templates upstream
- ✨ Apply changes → télécharge les nouveautés, supprime les obsolètes
- Le NFS reflète automatiquement chez les workers

### Stopper le cluster

```bash
for h in dg1@dg1 dg2@dg2 dg3@dg3 dg4@dg4; do
    ssh $h 'kill $(lsof -ti:9000) $(lsof -ti:8188) 2>/dev/null || true'
done
```

## ⚠ Gotchas

1. **NFS lock** : le mécanisme `.partial` + `os.replace` du workflow-manager fonctionne
   sur NFS (rename atomique posix). Mais évite que 2 hosts essaient de télécharger le
   même fichier simultanément (utilise `--primary` exclusif).

2. **Latence GPU** : NFS gigabit suffit pour de l'inférence, mais avec des modèles
   30+ GB, le 1er load après reboot peut prendre 30s+. Considère un cache local
   sur les workers (rclone vfs cache, etc.) si critique.

3. **Permissions** : le `no_root_squash` dans `/etc/exports` est dangereux si
   les hosts ne sont pas de confiance. Préfère un user dédié `comfyui` avec UID
   identique sur tous les hosts.

4. **Quota** : NFS ne gère pas les quotas par user → garde un œil sur
   `df -h /mnt/cluster_models` ou utilise un FS avec quota natif.
