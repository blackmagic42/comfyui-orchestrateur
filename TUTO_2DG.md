# Tutoriel — Déployer ComfyUI sur dg2 + dg4 depuis ta machine

Tutorial pas à pas pour lancer `deploy_2dg.sh` qui automatise tout : auth SSH par
clé, install ComfyUI + orchestrator sur les 2 DGX, mode pool (catalogue partagé),
téléchargement parallèle.

## 🎯 Ce que ça fait

À partir de **2 mots de passe SSH** (`DG2dg2` et `DG4dg4`), le script :

1. Génère une **clé SSH dédiée** sur ta machine
2. **Push la clé** sur les 2 DG (1 seule utilisation des passwords via `sshpass`)
3. Configure `~/.ssh/config` pour utiliser cette clé automatiquement
4. **Install ComfyUI** sur dg2 + dg4 en parallèle (10 min)
5. **Setup le pool** : NFS exports + cross-mounts + mergerfs union
6. **Build le manifest** des modèles (sur dg2 = primary)
7. **Distribute le download** : dg2 télécharge 50% du catalogue, dg4 télécharge l'autre 50%
8. À la fin, **chaque DG voit le catalogue complet** sans dupliquer les modèles

Architecture finale :

```
   Toi (laptop)
      │
      │ SSH clé (auto via ~/.ssh/config)
      ├──────────────────┬──────────────────┐
      ▼                  ▼                  │
   dg2:9000 ◄──NFS──► dg4:9000              │
   (primary)          (worker)              │
   /data/local_shard  /data/local_shard     │
   ~750 GB            ~750 GB               │
                                            │
   ~/comfyui/models/ = mergerfs union ──────┘
       (~1.5 TB visible des 2 côtés)
```

## 📋 Pré-requis sur ta machine (le poste de contrôle)

### Linux / macOS
```bash
# Vérifie que tu as bash + ssh + rsync (déjà là 99% du temps)
bash --version
ssh -V
rsync --version

# Installe sshpass (pour push la clé via password 1 fois)
# Ubuntu/Debian
sudo apt install sshpass

# macOS (via brew)
brew install hudochenkov/sshpass/sshpass
```

### Windows (Git Bash)
```bash
# Avec MSYS2
pacman -S sshpass

# OU mieux : utilise WSL Ubuntu et lance depuis là
wsl -d Ubuntu
sudo apt install sshpass
```

## 📋 Pré-requis sur dg2 et dg4

Chaque DG doit avoir :
- **OpenSSH server** (déjà là sur Ubuntu/Debian par défaut)
- **Le compte `dg2` (ou `dg4`)** avec les passwords donnés
- **`sudo` sans password** ou avec un password connu (pour NFS exports)
- **`python3`, `git`, `nvidia-smi`, `nfs-kernel-server`, `mergerfs`** (le script tente de les installer si admin)
- **Connectivité réseau** entre dg2 et dg4 (LAN ou subnet privé)

> 💡 Si les DG sont sur un sous-réseau privé non-`192.168.0.0/16`, exporte
> la variable d'env `POOL_SUBNET=10.0.0.0/8` (ou ce qui correspond) avant de lancer.

## 🚀 Lancement — étape par étape

### 1. Récupère le repo sur ta machine

```bash
git clone <ce-repo> ~/comfyui-stack
cd ~/comfyui-stack
ls scripts/
# → deploy_2dg.sh, deploy_cluster.sh, install_comfyui.sh, ...
```

### 2. Vérifie que les DG sont joignables

```bash
ping -c 2 dg2
ping -c 2 dg4
```

> 💡 Si `dg2` n'est pas résolu par DNS, ajoute dans `/etc/hosts` :
> ```
> 192.168.1.42  dg2
> 192.168.1.44  dg4
> ```

### 3. Lance le script — full deploy

```bash
bash scripts/deploy_2dg.sh
```

Le script va :
1. Te générer une clé SSH dans `~/.ssh/comfyui_dg_cluster`
2. Pusher la clé sur les 2 DG (utilise les passwords automatiquement)
3. Demander `sudo` sur les DG (ils peuvent te demander la confirmation)
4. Install + setup pool + download → ~30 min total

### 4. Surveille les logs (fenêtre séparée)

Pendant le déploiement, ouvre un autre terminal :

```bash
# Voir les downloads en temps réel sur dg2
tail -f /tmp/dl_dg2_dg2.log

# Voir l'orchestrator de dg2
ssh dg2@dg2 'tail -f ~/.catalog_state/orchestrator.log'
```

### 5. Accède au dashboard

À la fin du script, il affiche :

```
🎛 Dashboards :
    http://dg2:9000/dashboard
    http://dg4:9000/dashboard
🔑 Token (paste in dashboard) : XX6IUTTz8r8nQAFuwnma...
```

Ouvre `http://dg2:9000/dashboard` (ou via Tailscale si dg2 est sur ton VPN), colle le token, et tu vois :
- Les 218 workflows classés
- L'état des téléchargements en live
- L'onglet **⚙ Commands** pour gérer le cluster

## 🔧 Modes partiels (avancé)

Tu peux skipper certaines étapes si déjà faites :

```bash
# Bootstrap SSH uniquement (push la clé, configure ~/.ssh/config, sort)
bash scripts/deploy_2dg.sh --bootstrap-only

# Skip le bootstrap (clé déjà en place)
bash scripts/deploy_2dg.sh --skip-bootstrap

# Skip l'install ComfyUI (déjà fait)
bash scripts/deploy_2dg.sh --skip-deploy

# Skip le pool setup (déjà configuré)
bash scripts/deploy_2dg.sh --skip-pool

# Skip le download (juste setup)
bash scripts/deploy_2dg.sh --skip-download

# Budget custom (défaut: 1500 GB pour le pool 2 DG = 750 GB chacun)
bash scripts/deploy_2dg.sh --budget 1000
```

## 🔍 Vérification post-deploy

```bash
# Pool occupancy
bash scripts/deploy_cluster.sh pool-status \
    --hosts "dg2@dg2 dg4@dg4"

# Test depuis ta machine
curl http://dg2:9000/dashboard
curl http://dg4:9000/dashboard

# Liste les modèles vus par dg2
ssh dg2@dg2 'ls ~/comfyui/models/checkpoints/ | head'
```

## ⚠ Troubleshooting

### "sshpass: command not found"
Installe-le ou utilise le bootstrap manuel :
```bash
# 1. Affiche ta pubkey
cat ~/.ssh/comfyui_dg_cluster.pub

# 2. Sur chaque DG (avec password), ajoute-la :
ssh dg2@dg2  # password : DG2dg2
mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys
# Colle la pubkey, Ctrl+D

ssh dg4@dg4  # password : DG4dg4
# idem

# 3. Re-lance avec --skip-bootstrap
bash scripts/deploy_2dg.sh --skip-bootstrap
```

### "Permission denied (publickey)"
La clé n'a pas été pushée correctement.
```bash
# Vérifie ce que la pubkey contient sur dg2
ssh dg2@dg2 'cat ~/.ssh/authorized_keys'
# Doit inclure la ligne de ~/.ssh/comfyui_dg_cluster.pub
```

### "sudo: no tty present"
Les commandes NFS exports ont besoin de sudo. Sur les DG, configure :
```bash
ssh dg2@dg2
sudo visudo
# Ajoute la ligne :
# dg2 ALL=(ALL) NOPASSWD: /usr/sbin/exportfs, /bin/mount, /bin/umount, /usr/bin/tee
```

Ou autorise le sudo sans password complet (moins sûr) :
```bash
echo "dg2 ALL=(ALL) NOPASSWD:ALL" | sudo tee /etc/sudoers.d/dg2
```

### "mergerfs: command not found"
Le script tente `apt install mergerfs` automatiquement. Si ça échoue, fallback automatique en mode **symlink farm** (fonctionne mais avec gotchas, voir `CLUSTER.md`).

Pour forcer l'install manuel :
```bash
ssh dg2@dg2 'sudo apt install -y mergerfs'
ssh dg4@dg4 'sudo apt install -y mergerfs'
bash scripts/deploy_2dg.sh --skip-bootstrap --skip-deploy --skip-download
```

### Le download bloque
Vérifie que ComfyUI tourne sur chaque DG (l'orchestrator l'auto-launch au 1er job, mais peut-être bloqué) :
```bash
ssh dg2@dg2 'lsof -i :8188'
# Si vide, lance manuellement :
ssh dg2@dg2 'cd ~/comfyui && python main.py --listen 0.0.0.0 --port 8188 --use-pytorch-cross-attention &'
```

## 📊 Aperçu du résultat final

```
$ bash scripts/deploy_cluster.sh pool-status --hosts "dg2@dg2 dg4@dg4"

═══ Pool status ═══
HOST                 LOCAL_SHARD     MODELS_VIEW     FILES
──────────────────────────────────────────────────────────────────
dg2@dg2              745G            1.4T            136
dg4@dg4              754G            1.4T            136
```

→ Chaque DG occupe ~750 GB localement mais voit 1.4 TB de modèles.

## 🔄 Maintenance

### Mettre à jour le catalogue (nouveaux modèles upstream)
Depuis le dashboard de **dg2** :
- Onglet ⚙ Commands → 🔄 Sync (fetch + rebuild)
- Puis ✨ Apply changes

Ou en ligne de commande :
```bash
bash scripts/deploy_2dg.sh --skip-bootstrap --skip-deploy --skip-pool
# (ne fait que le re-download des nouveaux modèles)
```

### Stopper tout

```bash
ssh dg2@dg2 'kill $(lsof -ti:9000) $(lsof -ti:8188) 2>/dev/null || true'
ssh dg4@dg4 'kill $(lsof -ti:9000) $(lsof -ti:8188) 2>/dev/null || true'
```

### Re-déployer un script modifié

Si tu modifies un script local :
```bash
bash scripts/deploy_2dg.sh --skip-bootstrap --skip-pool --skip-download
# Re-rsync les scripts sur les 2 DG, relance les orchestrateurs
```

## 🎓 Récap des commandes

| But | Commande |
|---|---|
| **Tout déployer (1ère fois)** | `bash scripts/deploy_2dg.sh` |
| Push clé SSH uniquement | `bash scripts/deploy_2dg.sh --bootstrap-only` |
| Re-déploy après modif scripts | `bash scripts/deploy_2dg.sh --skip-bootstrap --skip-pool --skip-download` |
| Status du pool | `bash scripts/deploy_cluster.sh pool-status --hosts "dg2@dg2 dg4@dg4"` |
| Re-download nouveaux modèles | `bash scripts/deploy_2dg.sh --skip-bootstrap --skip-deploy --skip-pool` |
| Stop tout | `ssh dgN 'kill $(lsof -ti:9000) $(lsof -ti:8188)'` |
