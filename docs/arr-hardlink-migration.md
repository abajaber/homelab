# Runbook: collapse the arr datasets so hardlinks work

**Status: not yet performed.** Moves ~1.8 TB and ends in a `zfs destroy`. Read
the whole thing before starting. Every path stays byte-identical, so no compose
file, Sonarr root folder, qBittorrent path or SMB share needs changing.

## The problem

`media` and `torrents` are separate ZFS datasets nested under `redsea/arr`:

```
redsea/arr             used=  1.8T   usedByDataset=287.7K   dev=63   <- holds only the two mountpoints
redsea/arr/media       used=776.5G   usedByDataset=776.5G   dev=64
redsea/arr/torrents    used=  1.0T   usedByDataset=  1.0T   dev=65
```

A hardlink is a second directory entry pointing at one **inode**, and inode
numbers are only unique within a single filesystem. Three device IDs means three
filesystems, so `link()` across them returns `EXDEV`. Sonarr has
`copyUsingHardlinks: true`, gets `EXDEV`, and silently falls back to a full byte
copy — no error, no health warning.

The compose mount is already correct (`/mnt/redsea/arr:/data`, TRaSH's
single-mount layout) and cannot help: Docker binds are recursive, so the child
mountpoints and their device IDs travel into the container intact. Nothing above
the filesystem can merge two filesystems.

Proof, from a file Sonarr imported on 2026-08-04:

```
/mnt/redsea/arr/torrents/anime/BLEACH...S01E30...-VARYG.mkv   dev=65 inode=94  nlink=1  1.14GB
/mnt/redsea/arr/media/anime/Bleach (2004).../S17E30...mkv     dev=64 inode=139 nlink=1  1.14GB
```

`nlink=1` on both: two independent copies of identical bytes. Linked files would
share one inode and report `nlink=2`.

## What you get

- Imports become instant metadata operations instead of multi-GB copies. No more
  window where Jellyfin can see a half-written file.
- Seeding stops costing a second copy of every file *going forward*.
- Existing pairs are **not** deduplicated by the move — see the optional
  `jdupes` pass at the end to reclaim the ~776 GB already duplicated.

## Before you start

- **Free space**: needs ~1.8 TB transient (both copies exist between steps 4 and
  7). Last checked: 32.6 TB available. Re-check with `zfs list redsea`.
- **Downtime**: everything below is stopped for the duration of the copy —
  several hours for 1.8 TB. Run it overnight.
- **Shell access**: TrueNAS UI → *System Settings → Shell*, or SSH. `scripts/ot.py`
  **cannot** do this — open-terminal only bind-mounts `/mnt/redsea/apps`, so it
  cannot see `/mnt/redsea/arr` at all.
- **No snapshot tasks or replication** reference these datasets (verified
  2026-08-04), so nothing breaks on that front.
- **SMB share on `/mnt/redsea/arr/torrents`** exists and must keep resolving.
  It is path-based, so it survives — but verify it in step 8.

### Apps that bind these paths — all must be stopped

Grepped from `servers/truenas/apps/*/compose.yml`:

| app | paths |
|---|---|
| `arr` | `/mnt/redsea/arr` |
| `qbittorrent-vpn` | `/mnt/redsea/arr/torrents` |
| `qbittorrent-direct` | `/mnt/redsea/arr/torrents` |
| `jellyfin` | `/mnt/redsea/arr/media` |
| `navidrome` | `/mnt/redsea/arr/media/music` |
| `music-tools` | `/mnt/redsea/arr`, `/mnt/redsea/arr/media/music`, `/mnt/redsea/arr/torrents/music` |

Missing one means writes land in the old dataset mid-copy and are lost at step 7.

## Steps

### 1. Stop the apps

TrueNAS UI → *Apps* → stop each of the six above. Confirm none are running:

```sh
docker ps --format '{{.Names}}' | sort
```

Expect no `sonarr`, `sonarr-tv`, `radarr`, `bazarr`, `prowlarr`, `qbittorrent*`,
`gluetun`, `jellyfin`, `navidrome`, or music-tools containers.

### 2. Record the "before" state, to compare against later

```sh
zfs list -o name,used,available redsea/arr redsea/arr/media redsea/arr/torrents
find /mnt/redsea/arr/media    -type f | wc -l
find /mnt/redsea/arr/torrents -type f | wc -l
du -sh /mnt/redsea/arr/media /mnt/redsea/arr/torrents
```

Keep this output. Step 6 compares against it.

### 3. Check whether `mountpoint` is inherited — this changes the commands

```sh
zfs get -o property,value,source mountpoint redsea/arr/media redsea/arr/torrents
```

- **source `default` or `inherited`** (expected): `zfs rename` moves the
  mountpoint automatically. Proceed as written.
- **source `local`**: the dataset stays mounted at the old path after a rename
  and will block step 4's `mkdir`. Fix each with an explicit set after renaming:
  `zfs set mountpoint=/mnt/redsea/arr-media-old redsea/arr-media-old`

### 4. Rename the datasets out of the way

Metadata only, no data moves, seconds to run.

```sh
zfs rename redsea/arr/media    redsea/arr-media-old
zfs rename redsea/arr/torrents redsea/arr-torrents-old
zfs list -r redsea | grep arr
```

`/mnt/redsea/arr` should now be an empty directory inside its own dataset, with
the data at `/mnt/redsea/arr-media-old` and `/mnt/redsea/arr-torrents-old`.

### 5. Create real directories and copy the data in

```sh
mkdir -p /mnt/redsea/arr/media /mnt/redsea/arr/torrents
chown apps:apps /mnt/redsea/arr/media /mnt/redsea/arr/torrents
chmod 770       /mnt/redsea/arr/media /mnt/redsea/arr/torrents

rsync -aHAX --info=progress2 /mnt/redsea/arr-media-old/    /mnt/redsea/arr/media/
rsync -aHAX --info=progress2 /mnt/redsea/arr-torrents-old/ /mnt/redsea/arr/torrents/
```

Trailing slashes matter — they copy *contents*, not the directory itself.
`-aHAX` preserves ownership, permissions, ACLs, xattrs and any existing
hardlinks. Run it in `tmux`/`screen` so an SSH drop doesn't kill it.

### 6. Verify before destroying anything

```sh
find /mnt/redsea/arr/media    -type f | wc -l   # must match step 2
find /mnt/redsea/arr/torrents -type f | wc -l   # must match step 2
du -sh /mnt/redsea/arr/media /mnt/redsea/arr/torrents

# byte-for-byte structural comparison; must print nothing
rsync -aHAXn --delete --itemize-changes /mnt/redsea/arr-media-old/    /mnt/redsea/arr/media/
rsync -aHAXn --delete --itemize-changes /mnt/redsea/arr-torrents-old/ /mnt/redsea/arr/torrents/

# the whole point: these two must now report the SAME dev
stat -c '%d %i %h %n' /mnt/redsea/arr/media /mnt/redsea/arr/torrents
```

**Do not continue until the dev numbers match and the dry-run rsyncs are
silent.** Everything up to here is fully reversible — rename the old datasets
back and delete the new directories.

### 7. Destroy the old datasets — the irreversible step

```sh
zfs destroy -r redsea/arr-media-old
zfs destroy -r redsea/arr-torrents-old
zfs list -r redsea | grep arr
```

If you would rather keep a safety net, skip this until the apps have run
correctly for a day. The cost is 1.8 TB of space held.

### 8. Restart and verify

Start the six apps. Then:

- **Sonarr** → *Settings → Media Management* → root folder shows as accessible;
  `System → Status` has no new health warnings.
- **qBittorrent** (both) → torrents resume seeding, not "missing files". Paths
  are unchanged, so they should re-check and continue.
- **SMB share** on `/mnt/redsea/arr/torrents` → still browsable.
- **Jellyfin / Navidrome** → libraries still populated.

Then prove hardlinks actually work now, with a real import:

```sh
# after Sonarr imports one episode
stat -c 'dev=%d inode=%i nlink=%h  %n' \
  '/mnt/redsea/arr/torrents/anime/<the release>.mkv' \
  '/mnt/redsea/arr/media/anime/<the imported file>.mkv'
```

Success is **one shared inode and `nlink=2`**, versus the two distinct inodes at
`nlink=1` documented at the top of this file.

## Optional: reclaim the already-duplicated space

The move copies files; it does not link the existing media/torrent pairs. They
stay as two copies until re-imported. To relink them in place:

```sh
jdupes -r -L /mnt/redsea/arr/media /mnt/redsea/arr/torrents   # -L replaces duplicates with hardlinks
```

Run `jdupes -r` (no `-L`) first to see what it would touch. This only works
after the migration — before it, the files are on different filesystems and
`jdupes -L` cannot link them either. Expect to recover a large fraction of the
776 GB media side.

## If it goes wrong

Before step 7, recovery is a rename away:

```sh
rm -rf /mnt/redsea/arr/media /mnt/redsea/arr/torrents
zfs rename redsea/arr-media-old    redsea/arr/media
zfs rename redsea/arr-torrents-old redsea/arr/torrents
```

After step 7 there is no undo — the old datasets are gone. That is the only
reason step 6 is written as a hard gate.
