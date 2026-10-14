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
`STORAGE_DIR/.cluster/<instance_id>` on each heartbeat; when an instance sees fresh markers of
every other live instance the directory is one shared mount → `ok`. Markers that are missing or
stale → `warn` naming the instances whose files are invisible here. (With one live instance the
check cannot decide and stays `warn`.) Then upload a theme or an ITSM attachment through the load
balancer and fetch it again a few times — every instance must serve it.

## Known limits

- No object-store driver yet (`LYNDRIX_STORAGE_DRIVER=s3` with MinIO/Garage/S3 is the roadmap item);
  today "shared" means one mounted directory.
- The core writes `/app/logs/lyndrix.log` on every instance — do **not** share that directory;
  rely on stdout/`docker logs` instead.
- NiceGUI's per-user storage (`/app/.nicegui`) is process-bound; irrelevant with `react`.
