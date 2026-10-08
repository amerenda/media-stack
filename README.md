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

## Host prerequisites (Ansible, not this repo)

Config dirs under `/opt/*-config` (uid 1000), `/mnt/storage/media/config/{calibre/config,bookorbit/{data,imports}}`
(uid 1000), `bookorbit/postgres` (uid 999), and the `/mnt/storage/books/library/*` tree.
