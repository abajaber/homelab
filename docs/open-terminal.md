# open-terminal: read/write live state on TrueNAS without SSH

`servers/truenas/apps/open-terminal/` runs the [open-webui/open-terminal](https://github.com/open-webui/open-terminal) REST API on TrueNAS at `https://open-terminal.bajaber.ca`. The container runs as `apps:apps` (UID 568) with `/mnt/redsea/apps:/mnt/apps` bind-mounted RW, so any tool with the API key can read/write every per-app folder on the host with the same identity TrueNAS itself uses.

It's the fastest way to peek at on-disk state during troubleshooting — meaningfully cheaper than SSHing into TrueNAS and remembering the dataset path.

## When to reach for it

- **Troubleshooting a live container.** Read generated state the repo doesn't track: `arr/<name>/config/config.xml` (API keys, DB rows), `qbittorrent-*/config/qBittorrent/qBittorrent.conf`, sqlite DBs, app logs in `/mnt/apps/<app>/<sub>/logs/`.
- **Reading what's actually on disk** when live state diverges from what compose suggests — confirming a permissions fix landed, checking a sidecar wrote what it was supposed to, comparing `.env` substitution against what the container sees.
- **One-shot ops the reconciler doesn't cover** — chowning a path, creating a sibling folder for a hand-tuned bind, dropping a placeholder file before first deploy.

## When NOT to use it

- **Don't edit `compose.yml`, `app.yml`, or `.env`** via this surface. Those live in the repo under `servers/<host>/apps/<app>/` and are pushed by the reconciler. Edits made directly on the bind-mounted folders aren't seen by the apps reconciler (it only reads the repo) and won't survive the next apply if the live compose drifts.
- **Don't use it as a backdoor for secrets.** Secrets belong in the per-app vault-encrypted `.env`. The web terminal can read the live cleartext value (the container has it expanded), but writing a new value here doesn't update the vault — and the next apply will overwrite whatever you wrote.

## Auth

The API key lives in `servers/truenas/apps/open-terminal/.env` (vault-encrypted). For convenience, `.open-terminal.env` at the repo root is a gitignored cleartext copy with the key + base URL.

Regenerate it from the vault if it's missing:

```bash
KEY=$(ansible-vault view servers/truenas/apps/open-terminal/.env | grep ^OPEN_TERMINAL_API_KEY | cut -d= -f2)
printf 'OPEN_TERMINAL_URL=https://open-terminal.bajaber.ca\nOPEN_TERMINAL_API_KEY=%s\n' "$KEY" > .open-terminal.env
chmod 600 .open-terminal.env
```

Load into the current shell:

```bash
set -a; . ./.open-terminal.env; set +a   # OPEN_TERMINAL_URL + OPEN_TERMINAL_API_KEY in env
```

## The `scripts/ot.py` wrapper

Six verbs, no flags except `--timeout` on `exec`. Walks up from CWD to find `.open-terminal.env`, so it works from any subdir of the repo.

```bash
./scripts/ot.py ls /mnt/apps                                              # one entry per line
./scripts/ot.py cat /mnt/apps/arr/sonarr/config/config.xml                # raw content to stdout
printf 'new content\n' | ./scripts/ot.py write /mnt/apps/x/y              # stdin → file
./scripts/ot.py replace /mnt/apps/x/y 'old' 'new'                         # single in-place swap
./scripts/ot.py grep 'AuthenticationMethod' /mnt/apps/arr                 # server-side ripgrep
./scripts/ot.py exec --timeout 10 'ls -la /mnt/apps/open-terminal/home'   # auto-polls; exits with remote code
```

Use the wrapper for the common case — it handles auth, strips the JSON envelope, and is meaningfully cheaper in tokens than raw curl.

## Raw curl

Drop to raw curl for things the wrapper deliberately doesn't cover (listing or killing detached execs, multi-target replaces, line-bounded reads, glob, anything else under `${OPEN_TERMINAL_URL}/docs`). Every endpoint takes `Authorization: Bearer ${OPEN_TERMINAL_API_KEY}`. The full API table is in [`CLAUDE.md`](../CLAUDE.md) under "Web terminal for direct on-box file ops".

```bash
. ./.open-terminal.env
H="Authorization: Bearer $OPEN_TERMINAL_API_KEY"
curl -sS -H "$H" "$OPEN_TERMINAL_URL/files/glob?pattern=*.xml&path=/mnt/apps/arr"
```
