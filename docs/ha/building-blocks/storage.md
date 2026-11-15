# Building block: shared storage

## Contract

Files that more than one instance must see: plugin attachments (ITSM), user background images,
uploaded themes, the plugin zip cache. Today these are written to the local file system under
`STORAGE_DIR` (`/data/storage`) and the image layer (themes).

## Options

| Option | HA of the layer | Notes | When |
|---|---|---|---|
| Local disk per instance | none | files written on A are not on B — acceptable only while no plugin stores user files | Tier 0, dev |
| **Shared mount** (NFS / CIFS / Kubernetes RWX volume) for `/data/storage` | that of the NAS/volume | works today without core changes; the dev test bench mounts the same host directory into both instances | Tier 1 now |
| Object store (MinIO, Garage, SeaweedFS, S3) via `LYNDRIX_STORAGE_DRIVER=s3` | yes | planned (`ctx.storage` abstraction with `local`/`fs`/`s3` drivers) | Tier 2 later |

## Lyndrix settings

`STORAGE_DIR`, `SECURITY_DIR` (the key file must be on every instance; a shared secret, not shared
storage). Planned: `LYNDRIX_STORAGE_DRIVER`, `LYNDRIX_STORAGE_S3_*`.

## Verify

The preflight `storage` check does it for you: every instance writes a marker file
`STORAGE_DIR/.cluster/<instance_id>` on each heartbeat that names the process (its *incarnation*)
which wrote it. An instance that can open a peer's marker **by name** and reads the incarnation
that peer's registry row carries has proof that both look at one directory → `ok`. The check does
not list the directory and does not look at file times (both are unreliable on NFS: listing and
attribute caches, server clock vs. reader clock).

- Only instances with a role in `LYNDRIX_STORAGE_SHARED_ROLES` (default `api` — they serve user
  files) have to share. A runner-only instance on another host is mentioned in the `ok` message
  ("not shared with … — fine") and reports `ok` itself.
- An instance with such a role whose marker is not visible → `warn`: *"/data/storage is NOT shared
  with docker-auto (api)"*. That is a real finding, not a glitch: uploads, attachments and uploaded
  themes written through one of the two do not exist on the other, so a user who is routed to both
  sees files come and go. Mount the same volume there, or keep that instance out of the
  user-facing load balancer.
- A peer that registered less than two heartbeats ago is not judged yet; a `warn` is re-checked two
  heartbeats later instead of staying for five minutes. With no second file-serving instance alive
  the check cannot decide and stays `warn`.
- The mount is only ever touched by one thread of the cluster layer; if it hangs, the check says
  *"… is not responding — a storage job has been stuck for N s"*.
- The leader removes marker files of instances that have been gone for more than a day.

Then upload a theme or an ITSM attachment through the load balancer and fetch it again a few
times — every instance must serve it.

## Known limits

- No object-store driver yet (`LYNDRIX_STORAGE_DRIVER=s3` with MinIO/Garage/S3 is the roadmap item);
  today "shared" means one mounted directory.
- The core writes `/app/logs/lyndrix.log` on every instance — do **not** share that directory;
  rely on stdout/`docker logs` instead.
- NiceGUI's per-user storage (`/app/.nicegui`) is process-bound; irrelevant with `react`.
