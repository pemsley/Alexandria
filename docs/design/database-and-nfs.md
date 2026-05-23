# Database, sidecars, and what happens on NFS

A reference for "how does Alexandria stay safe when more than one
process — possibly on more than one host — touches the same library?"

## TL;DR

* The SQLite index DB **never** crosses hosts. Each host has its own,
  built from the sidecars.
* The sidecar JSON files **are** the source of truth, written via
  the standard tmp-file + fsync + rename pattern.
* Reading sidecars is race-free.
* Writing the same sidecar from two hosts simultaneously is **not**
  protected — last writer wins.
* The watcher does **not** see changes made by another NFS client —
  inotify is local-only.
* Putting the DB on NFS isn't recommended, isn't blocked by the app,
  and is the user's (or admin's) responsibility to avoid.

## The DB is local-only by design

`index.py` opens the SQLite database at:

    $XDG_STATE_HOME/Alexandria/library.<host-hash>.db
    # default: ~/.local/state/Alexandria/library.<host-hash>.db

The `<host-hash>` is a 4-character `blake2s` digest of a **stable
host identifier**, picked in this order (see `_stable_host_id` in
`index.py`):

1. `/etc/machine-id` (Linux systemd).
2. `/var/lib/dbus/machine-id` (pre-systemd Linux).
3. macOS `IOPlatformUUID` via `ioreg -d2 -c IOPlatformExpertDevice`.
4. Last-resort sentinel file
   `$XDG_STATE_HOME/Alexandria/host-id` containing a UUID we
   generate on first launch.

The first three are host-specific even when `$HOME` is NFS-mounted
and shared — so each host gets its own DB filename and the two
files simply don't collide. This is the **guardrail against the
multi-host-on-shared-$HOME accident**: hostA launches Alexandria,
forgets to close it; the next day hostB launches Alexandria against
the same `$HOME`. Without per-host filenames, both processes would
open the same DB over NFS and the WAL would corrupt silently. With
per-host filenames, each just opens its own file and the worst case
is duplicated import work, not a corrupted index.

### Why not `socket.gethostname()`

The initial implementation hashed `socket.gethostname()`. That turns
out to be **not stable on macOS** — the hostname returned by
`gethostname()` changes when the machine joins a different network
(mDNS picks a new `<name>.local`), gets a new DHCP lease, or wakes
from sleep on a foreign LAN. A drifting hostname means a drifting
DB filename, which made the user's library appear to "go missing"
on a different network even on a single-machine install. The
sources listed above are stable: `machine-id` is generated at
install time; `IOPlatformUUID` is set in the hardware at
manufacturing time.

### Sentinel fallback caveat

The sentinel-UUID fallback (#4) is *not* host-specific when `$HOME`
is shared — two hosts reading the same sentinel will compute the
same hash and open the same DB, defeating the multi-host
protection. We accept this because the platforms where NFS-shared
`$HOME` is realistic (Linux server installs, university cluster
login nodes) all have `/etc/machine-id`, and macOS has
`IOPlatformUUID`. The sentinel only kicks in on something exotic
where neither is available, in which case the user almost certainly
isn't sharing `$HOME` across machines anyway.

The comment at the top of `index.py` is explicit:

> Local SQLite index — a regeneratable cache. The truth lives in
> sidecars. DB lives on local disk, never on NFS.

Two hosts editing the same library each have their own DB. There is
no DB-level synchronisation between them, and no SQLite write
contention either. A lost DB can be rebuilt by walking the library
and re-importing each PDF; nothing irreplaceable lives there.

### Legacy / stale-hash adoption

`_migrate_legacy_db()` runs in `open_db` before the connect, and
adopts an existing DB into the current stable-host-hashed name:

- Pre-host-hash installs (`library.db`) — renamed once.
- Pre-stable-host-id installs (`library.<old-hostname-hash>.db`,
  from the brief window where the hash was driven by
  `gethostname()`) — also renamed once.

If exactly one candidate is found, it's renamed (with its `-wal` /
`-shm` companions). If multiple candidates are found, the migrator
refuses to guess and logs to stderr; the user moves the right one
manually. If no candidates exist (fresh install), nothing happens.

The connection is opened with:

```python
conn = sqlite3.connect(path, check_same_thread=False)
conn.execute("PRAGMA journal_mode=WAL")
conn.execute("PRAGMA synchronous=NORMAL")
```

`check_same_thread=False` lets the GUI thread share the connection
with background import / citation-refresh threads (SQLite serialises
access internally). `WAL` lets readers proceed while a writer holds
the write lock — important for the responsiveness of the GUI while
imports run.

### Caveat: NFS-mounted homes

If `$HOME` is itself NFS-mounted, the default path lands on NFS too.
The per-host filename means two hosts won't share a DB file, but a
single host's DB still gets the unreliable end of NFS file locking.
A one-shot warning toast fires when the cache lives on NFS / SMB /
sshfs. The user has two escape hatches, both environment-level:

* `XDG_STATE_HOME=/tmp/<user>-state` (or any local-disk path) before
  launch.
* Symlink `~/.local/state/Alexandria` to a local directory.

Putting the DB on NFS works on a single host but loses the safety
properties WAL relies on (NFS file locking is famously unreliable),
and the cache rebuild on a missing/corrupt DB is cheap, so there is
no upside to it.

A future startup check could `statfs(2)` the DB path, compare
`f_type` against the NFS magic numbers (`0x6969`, `0xff534d42`),
and refuse-or-warn. Not currently implemented.

## Sidecars are the truth, atomically written

`sidecar.write` does:

```python
tmp = path + ".tmp"
with open(tmp, "w", encoding="utf-8") as f:
    json.dump(record, f, indent=2, ensure_ascii=False)
    f.flush()
    os.fsync(f.fileno())
os.rename(tmp, path)
```

POSIX `rename(2)` within the same directory is atomic — a reader on
either host always sees the old complete sidecar or the new complete
sidecar, never a partial one. NFSv3 and later preserve that
atomicity. **Reads are race-free.**

`fsync` after the write means the bytes are on stable storage before
the rename, so a crash mid-write can't leave a stale `path` pointing
at a freshly-truncated file.

## Cross-host visibility is via the watcher

When host A writes a sidecar, the local-host expectation is:

1. GFileMonitor on host A's library directory fires `_on_changed`
   for the new/modified `*.alexandria`.
2. The watcher re-reads the sidecar and upserts the local DB row.
3. The GUI redraws.

The same mechanism is what makes `alexandria-import --refresh`
invisibly update a running browser: the CLI rewrites the JSON, the
watcher sees it, the row is upserted, the GUI redraws.

This story breaks down across NFS clients. **inotify watches local
kernel inode events; it does not see writes from other NFS
clients.** So:

* Host A writes `foo.pdf.alexandria` to the share → host A sees
  it instantly.
* Host B's GFileMonitor stays silent until B does its own stat
  (refresh button, restart, reload). Eventually-consistent at best.

GLib has a polling fallback (`G_FILE_MONITOR_USE_GIO_POLL`,
FAM-style) that *would* see remote writes, but it's not on by
default and we do not force it.

## The races that exist for shared-library use

### Concurrent same-file writes — clobber

Both hosts use the same tmp filename `foo.pdf.alexandria.tmp`. If A
and B write the same record at the same wall-clock moment:

1. Both writers open the same tmp path.
2. Both `fsync`.
3. Both `rename`. The second rename wins; the first host's edit is
   silently lost.

There is no advisory lock — `fcntl(F_SETLK)` on NFS needs `lockd` /
`rpc.statd` and is brittle, so we do not use it.

### Concurrent first-import — duplicate work

When a new PDF appears on the share, both hosts' watchers may try
to import it simultaneously: extract → write sidecar → write thumb.

The `RECENT_THRESHOLD_SECONDS = 2.0` guard in `importer.import_pdf`
skips when the sidecar mtime is < 2 s old. This is mostly intended
to stop the *same* host's watcher firing twice (the importer's own
write triggers a CHANGED event on the file it just made), and only
narrowly helps cross-host: if A and B both stat at t=0 and find no
sidecar, both go and do the work, both rename their tmp at ~t=1,
the late rename wins. Network round-trip variance plus NFS attribute
caching means the 2-second window often isn't tight enough to catch
this.

## Summary table

| Scenario | Safe? |
|---|---|
| Two hosts, two separate DBs | Yes — by design |
| Reading a sidecar while another host is writing it | Yes — atomic rename |
| One host writing, another host eventually picking it up | Only if the other host triggers a stat. inotify won't fire. |
| Both hosts editing the same sidecar at the same instant | Race — one edit lost |
| Both hosts importing a freshly-arrived PDF at the same instant | Race — duplicate work, last rename wins |

## What we'd do to harden cross-host editing

Not implemented. Recorded here so it doesn't get re-derived from
scratch next time.

* **Polling watcher fallback.** Schedule a periodic `os.scandir` of
  the library and diff mtimes; layer it on top of the GFileMonitor
  signal. Catches remote-NFS sidecar writes that inotify misses.
* **Hostname-suffixed tmp paths for sidecars** — *shipped*.
  `sidecar.write` now writes to `foo.pdf.alexandria.<host>.<pid>.tmp`
  instead of the shared `foo.pdf.alexandria.tmp`. Doesn't fix
  last-rename-wins but eliminates the corrupt-tmp variant where two
  writers stomp on each other's tmp file mid-flush.
* **Read-modify-write with mtime check before rename.** If the
  sidecar's mtime changed between the read and the rename, abort
  and re-merge. Catches the common case.
* **Don't try to use SQLite as a shared truth on NFS.** Even with
  WAL, NFS file-locking flakiness ruins it. Sidecars-as-truth was
  the right call.

## Single-writer-at-a-time is the supported mode

Single user, single machine at a time, library on NFS or local —
all safe. Two active editors on two hosts is **not** currently safe.
