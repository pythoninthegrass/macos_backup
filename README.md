# macos_backup

A restic backup wrapper for macOS, backing up to a NAS over SFTP.

Backs up all of `$HOME` minus reinstallable runtimes/packages, cloud-synced folders, VMs, and caches (see `excludes.txt`). Keeps `~/Library/Keychains` (Apple signing certs), `~/Library/Preferences`, and a trimmed `~/Library/Application Support`. Also writes manifests for `brew bundle`, Dock apps, and `mise` runtimes on every run, so the dock and brewed packages can be restored even though they aren't backed up as files.

## Install

### Standalone (no install)

Run straight from a checkout — config (`.env`, `excludes.txt`) is read from the current directory either way.

```bash
./main.py --help
```

### Install from git

One-off execution:

```bash
uvx --from git+https://github.com/pythoninthegrass/macos_backup.git mb --help
```

Persistent install (drops `mb` into `~/.local/bin`):

```bash
uv tool install git+https://github.com/pythoninthegrass/macos_backup.git
mb --help
```

## Setup

Generate a repo password and save it to the login keychain (good idea to save it in a password manager.)

```bash
mb init
```

Grant Full Disk Access (System Settings > Privacy & Security > Full Disk Access) to whichever Python `mb` resolves to — the pre-flight check prints the exact path if this is missing. Re-grant after a `uv tool install --force`/upgrade changes that path.

Install and load the LaunchAgent (daily at 12:30). `WorkingDirectory` is pinned to this checkout, where `.env`/`excludes.txt`/`.tool-versions` live.

```bash
mb install
```

Settings come from `.env` in the current directory (copy `.env.example`), or `DOTENV_FILE` to point elsewhere, or the matching environment variables, or the defaults baked into `macos_backup/cli.py`. See `.env.example` for the full key list.

`mb` reads `.env`/`excludes.txt` from whatever directory it's run from, so setup and scheduled runs above still happen from this checkout.

### Usage

```bash
mb backup --dry-run   # preview what would be sent
mb backup             # run a backup (writes manifests, prunes on Sundays)
mb snapshots
mb check              # restic check --read-data-subset=5%
mb forget             # apply the keep policy + prune, on demand
```

