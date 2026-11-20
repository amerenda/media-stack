# media-stack

Komodo-managed Docker Compose stack on **murderbot**: Jellyfin, the *arr suite,
sabnzbd, seerr, maintainerr, shelfarr/BookOrbit, and the legacy calibre stack.
TLS/ingress is k3s Traefik + cert-manager (not part of this repo).

Migrated from `komodo-dean-gitops/murderbot/media-server`.

## Layout

| Path | Purpose |
|------|---------|
| `compose.yaml` | The stack. Project name `media-server` (kept so existing containers/networks are adopted). |
| `paths.env` | Non-secret host paths and settings (Komodo `additional_env_files`). |
| `secrets.yaml` | BWS secret UUID -> name map. |
| `pre-deploy.sh` | Reads `secrets.yaml`, writes `.env`. Nothing else. |
| `config/` | Versioned config mounted/copied into containers. |
| `scripts/` | Host-run helpers (not deployed). |

## Secrets

`secrets.yaml` maps `<BWS UUID>: <BWS name>`. `pre-deploy.sh` writes each as
`UPPER_SNAKE(name)=value` to `.env`; compose references `${UPPER_SNAKE}`.

## Komodo stack settings

- server: `murderbot`, repo `amerenda/media-stack`, branch `main`
- `file_paths = ["compose.yaml"]`, `project_name = "media-server"`
- `additional_env_files = ["paths.env"]`, `extra_args = ["--remove-orphans"]`
- `webhook_force_deploy = true` and a GitHub deploy webhook for the stack
- pre_deploy: `export BWS_ACCESS_TOKEN=$(cat /run/secrets/bws-access-token); bash pre-deploy.sh`

## Maintainerr rules

`config/maintainerr/rules.toml` is the source of truth for the Maintainerr rule
groups and job schedules. The one-shot service `maintainerr-sync`
(`scripts/maintainerr-sync.py`) writes it to Maintainerr on every deploy. A change
in the Maintainerr UI is overwritten: change the file and merge a PR.

- An item with the tag `keep` in Sonarr or Radarr is never deleted. Each rule
  section must end with `AND <Sonarr|Radarr>.tags NOT_CONTAINS keep`; the sync
  stops before any write if one does not.
- The sync updates the groups in the file only. It does not create or delete
  groups, and it stops if Maintainerr has a group that is not in the file.
- Dry run from a shell with access to Maintainerr:
  `MAINTAINERR_API_KEY=... MAINTAINERR_URL=http://10.100.20.19:6246/api RULES_FILE=config/maintainerr/rules.toml DRY_RUN=1 python3 scripts/maintainerr-sync.py`

## Host prerequisites (Ansible, not this repo)

Config dirs under `/opt/*-config` (uid 1000), `/mnt/storage/media/config/{calibre/config,bookorbit/{data,imports}}`
(uid 1000), `bookorbit/postgres` (uid 999), and the `/mnt/storage/books/library/*` tree.
