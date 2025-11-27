# Docker Network Watcher

A lightweight service that **watches Docker via the HTTP API** and **attaches/detaches networks to containers based on labels**.

- No direct `/var/run/docker.sock` mount required.
- Designed to work **behind a docker-socket-proxy**.
- Compatible with Docker 29+ (API 1.52); the API version is auto-detected at runtime.

---

## What this watcher does

- Listens to Docker **events** (`create`, `start`, `destroy`, etc.) and optionally runs a **periodic rescan**.
- Looks for container labels with a configurable **prefix** (default: `network-watcher.*`).
- For containers with such labels, it:
  - **Ensures networks exist** (and creates them if missing).
  - **Attaches** the container to the networks where the label value is truthy.
  - **Detaches** it from networks where the labels are false/removed.
  - Optionally **detaches all other networks** (detach-other mode).
- Supports **internal** vs **non-internal** networks via labels.
- Optionally **prunes unused networks** (those it managed for destroyed containers).
- Optionally **prunes orphan networks** (globally), with protections:
  - Never touches system networks.
  - Never touches networks still referenced by containers (`NetworkMode` / `Networks`).
  - Optional minimum age threshold.
  - Optional inclusion/exclusion of Docker Compose networks.
- **Never touches containers that have no label with the configured prefix.**

---

## High-level behaviour

1. On startup:
   - Determines the Docker API version from `/version` (fallback: `v1.52`).
   - Connects to Docker Engine over HTTP (`DOCKER_HOST`).
   - Logs its own version/build hash (if passed via env).
   - Optionally performs an **initial attach** on existing containers.

2. At runtime:
   - Streams events from `/events` and reacts to container lifecycle changes.
   - Optionally runs a periodic rescan every `RESCAN_SECONDS`.
   - For each container that has at least one label with the prefix (e.g. `network-watcher.*`):
     - Reads labels and computes:
       - **Desired networks** (`network-watcher.<net>: true/false`).
       - **Internal preferences** (`network-watcher.<net>.internal`).
       - **Alias** (`network-watcher.alias`).
       - **Detach-other override** (`network-watcher.detachdefault`).
     - Ensures networks exist / are internal/non-internal as requested.
     - Attaches / detaches networks accordingly.
     - Optionally detaches all non-labelled networks (detach-other).

3. Pruning:
   - When a container is **destroyed**, the watcher may prune networks it used to manage, if nobody else references them.
   - During periodic rescan, it may prune **orphan** networks:
     - Only if they are not system networks.
     - Only if they are not referenced by any container’s `NetworkMode` or `NetworkSettings.Networks`.
     - Only if they have no attached containers.
     - Optionally only if older than `NETWORK_WATCHER_PRUNE_ORPHAN_MIN_AGE`.
     - Optionally skipping or including Docker Compose networks.

---

## Label model

The label prefix is controlled by:

```env
NETWORK_WATCHER_PREFIX=network-watcher
````

All functional labels start with `<prefix>.`.

### 1. Attach / detach networks

```yaml
labels:
  network-watcher.traefikfront: "true"
  network-watcher.socketproxy: "true"
  network-watcher.psu: "false"
```

For this container:

* `network-watcher.traefikfront: true`
  → container **must be attached** to Docker network `traefikfront`.

* `network-watcher.socketproxy: true`
  → container **must be attached** to Docker network `socketproxy`.

* `network-watcher.psu: false`
  → network `psu` is considered **managed** for this container but **not desired**.
  If the container was previously attached to `psu` via labels, the watcher will disconnect it (if `AUTO_DISCONNECT=true`).

Networks *not* referenced in labels are left as-is, unless “detach-other” is enabled (see below).

> A container is handled by the watcher only if it has **at least one** label starting with `<prefix>.`.

---

### 2. Internal networks

You can enforce a network to be **internal**:

```yaml
labels:
  network-watcher.psu: "true"
  network-watcher.psu.internal: "true"
```

Behaviour:

* If network `psu` does **not exist**:

  * It will be created with:

    * `Name = psu`
    * `Driver = NETWORK_WATCHER_DEFAULT_DRIVER` (default: `bridge`)
    * `Internal = true`

* If `psu` already exists:

  * The watcher inspects it and checks the `Internal` field.
  * If it differs from the requested value:

    * Records the currently attached container IDs.
    * Tries to disconnect them (best effort).
    * Removes the network.
    * Recreates it with the same driver and the new internal flag.
    * Best-effort reattach of previously attached containers.

To explicitly enforce **non-internal**:

```yaml
labels:
  network-watcher.psu: "true"
  network-watcher.psu.internal: "false"
```

If `.internal` is omitted for a given network, the watcher **does not** try to change its `Internal` flag (it only influences creation when the network doesn’t exist).

---

### 3. Alias label

You can set an alias used when the watcher connects the container to networks:

```yaml
labels:
  network-watcher.traefikfront: "true"
  network-watcher.alias: "my-service"
```

All connections managed by the watcher for this container will use alias `my-service` on the networks.

If not specified, the alias defaults to the container name.

---

### 4. Detach-other mode (global + per-container)

Sometimes you want the watcher to **fully control which networks a container is on**, i.e. “if it’s not in labels → detach it”.

You can enable a **global** detach-other mode:

```env
NETWORK_WATCHER_DETACH_OTHER=true
```

Then, for a container like:

```yaml
labels:
  network-watcher.traefikfront: "true"
  network-watcher.socketproxy: "true"
```

The watcher will:

1. Ensure the container is attached to `traefikfront` and `socketproxy`.
2. Detach it from any other **non-system** network it’s attached to but that is **not** mentioned in `network-watcher.*` labels.

System networks are never detached by this feature:

* `bridge`
* `host`
* `none`
* `ingress`
* `docker_gwbridge`

#### Per-container override: `detachdefault`

If `NETWORK_WATCHER_DETACH_OTHER=true` globally, you can **opt out** per container:

```yaml
labels:
  network-watcher.detachdefault: "false"
```

For that container:

* Detach-other is disabled.
* The watcher still attach/detach networks based on labels (traefikfront, socketproxy, etc.).
* But it does **not** touch other networks attached to that container.

The override label name is configurable (see `NETWORK_WATCHER_DETACH_OVERRIDE_LABEL` below).

The watcher also detects itself via the container’s `HOSTNAME` and **never applies** detach-other to its own container.

---

## Pruning behaviour

There are two independent features:

1. **Per-container “unused network” prune**: networks previously managed for a container that’s being destroyed.
2. **Global orphan prune**: networks that nobody uses or references anymore.

Both are optional and configurable.

### 1. Prune unused networks (per destroyed container)

**Env:** `NETWORK_WATCHER_PRUNE_UNUSED_NETWORKS` (default: `true`)

When a container is **destroyed** (Docker event `destroy`):

1. The watcher retrieves the set of networks it previously managed for this container.
2. For each such network:

   * If it is a system network (`bridge`, `host`, `none`, `ingress`, `docker_gwbridge`) → **never delete**.
   * Computes a set of **referenced networks** across all containers:

     * `HostConfig.NetworkMode`
     * `NetworkSettings.Networks`
   * If the network name is still in this referenced set → **skip** (other containers still depend on it).
   * Inspects the network:

     * If `Containers` map is not empty → **skip** (still has active endpoints).
   * Otherwise:

     * Calls `DELETE /networks/<name>`.
     * Logs: `Removed unused network '<name>'`.

This is a targeted cleanup for networks the watcher knows it was managing for a container that no longer exists.

---

### 2. Prune orphan networks (global)

**Env:** `NETWORK_WATCHER_PRUNE_ORPHAN_NETWORKS` (default: `false`)

During each periodic rescan (if `RESCAN_SECONDS > 0`), if enabled, the watcher:

1. Lists all networks.
2. Computes the set of all **referenced network names**:

   * from every container’s `HostConfig.NetworkMode` and `NetworkSettings.Networks`.
3. For each network:

   * Skips system networks (`bridge`, `host`, `none`, `ingress`, `docker_gwbridge`).
   * Optionally skips Docker Compose networks, depending on `NETWORK_WATCHER_PRUNE_ORPHAN_INCLUDE_COMPOSE`.
   * If the network name is in the referenced set → **skip** (still used by containers, even stopped ones).
   * If the network has any attached containers in `Containers` → **skip**.
   * Optionally checks network “age” (`NETWORK_WATCHER_PRUNE_ORPHAN_MIN_AGE`).
   * Else:

     * Removes the network.
     * Logs: `Removed orphan network '<name>'`.

#### Orphan prune minimum age

**Env:** `NETWORK_WATCHER_PRUNE_ORPHAN_MIN_AGE` (default: e.g. `60` seconds)

This is a safety delay to avoid races where:

* A network is created (e.g. by Compose),
* The watcher sees it as empty before containers are attached,
* And could delete it too early.

If set, the watcher will only consider a network for orphan prune if:

* Its creation time (from `Created` / `CreatedAt` in the network inspect data) is **older than** `NETWORK_WATCHER_PRUNE_ORPHAN_MIN_AGE` seconds.

Set to `0` or a negative value to disable the age check.

#### Orphan prune & Docker Compose networks

**Env:** `NETWORK_WATCHER_PRUNE_ORPHAN_INCLUDE_COMPOSE` (default: `false`)

By default (`false`), the watcher is conservative and **skips Compose networks** when pruning orphans, even if they are not in use.

Compose networks are detected via:

* `Labels["com.docker.compose.network"]` (if present).

If you set:

```env
NETWORK_WATCHER_PRUNE_ORPHAN_INCLUDE_COMPOSE=true
```

then Compose networks are treated like any other:

* Can be pruned if:

  * Not system,
  * Not referenced by any container,
  * Have no attached containers,
  * Respect the min age (if set).

This is safe if you are comfortable with Compose re-creating the network on `docker compose up`, but the default is to act cautiously and leave them alone.

---

## Environment variables (full reference)

### Docker connection

| Variable      | Default                    | Description                                                                          |
| ------------- | -------------------------- | ------------------------------------------------------------------------------------ |
| `DOCKER_HOST` | `http://socket-proxy:2375` | HTTP(S) endpoint for Docker Engine. `unix://` is **not supported**; use a TCP proxy. |

If `DOCKER_HOST` starts with `tcp://`, it is normalized to `http://…`.
If it has no scheme, `http://` is prepended.

---

### Core runtime behaviour

| Variable                                | Default                  | Description                                                         |
| --------------------------------------- | ------------------------ | ------------------------------------------------------------------- |
| `NETWORK_WATCHER_PREFIX`                | `network-watcher`        | Label prefix. All watcher labels start with `<prefix>.`.            |
| `NETWORK_WATCHER_ALIAS_LABEL`           | `<prefix>.alias`         | Label key for network alias, e.g. `network-watcher.alias`.          |
| `NETWORK_WATCHER_DETACH_OVERRIDE_LABEL` | `<prefix>.detachdefault` | Label key used for per-container detach-other override.             |
| `INITIAL_ATTACH`                        | `true`                   | If `true`, reconcile containers on startup.                         |
| `INITIAL_RUNNING_ONLY`                  | `false`                  | If `true`, initial attach only scans running containers.            |
| `AUTO_DISCONNECT`                       | `true`                   | If `true`, detach networks when labels are set to false or removed. |
| `RESCAN_SECONDS`                        | `30`                     | Periodic rescan interval in seconds. `0` = disable periodic rescan. |
| `DEBUG`                                 | `false`                  | If `true`, enables verbose debug logging.                           |
| `NETWORK_WATCHER_DEFAULT_DRIVER`        | `bridge`                 | Default driver for newly created networks (when they don’t exist).  |

---

### Pruning options

| Variable                                       | Default | Description                                                                                                                                                  |
| ---------------------------------------------- | ------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `NETWORK_WATCHER_PRUNE_UNUSED_NETWORKS`        | `true`  | On container destroy, prune networks that were managed for that container, if they are not referenced by any other container and have no attached endpoints. |
| `NETWORK_WATCHER_PRUNE_ORPHAN_NETWORKS`        | `false` | During periodic rescan, prune orphan networks (no containers and not referenced by any container), subject to system/compose/min-age protections.            |
| `NETWORK_WATCHER_PRUNE_ORPHAN_MIN_AGE`         | `60`    | Minimum age in seconds for a network to be considered for orphan pruning. `0` or negative = no age check.                                                    |
| `NETWORK_WATCHER_PRUNE_ORPHAN_INCLUDE_COMPOSE` | `false` | If `false`, Compose networks (`com.docker.compose.network` label) are excluded from orphan pruning. If `true`, they are treated like any other network.      |

System networks (`bridge`, `host`, `none`, `ingress`, `docker_gwbridge`) are **never** pruned by the watcher.

---

### Detach-other (network isolation by labels)

| Variable                                | Default                  | Description                                                                                                                                                           |
| --------------------------------------- | ------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `NETWORK_WATCHER_DETACH_OTHER`          | `false`                  | If `true`, for containers with watcher labels, detach all non-system networks not referenced in labels (unless overridden by `detachdefault=false` or self-detected). |
| `NETWORK_WATCHER_DETACH_OVERRIDE_LABEL` | `<prefix>.detachdefault` | Label key used to override detach-other per container. `false` disables detach-other for that container.                                                              |

The watcher also reads `HOSTNAME` to detect its own container ID prefix and never applies detach-other to itself.

---

### Version / build metadata

| Variable             | Default                   | Description                                      |
| -------------------- | ------------------------- | ------------------------------------------------ |
| `WATCHER_VERSION`    | `dev`                     | Version string printed at startup.               |
| `WATCHER_BUILD_HASH` | `unknown` or `GIT_COMMIT` | Build hash (e.g. git commit) printed at startup. |

Example startup log:

```text
Starting docker-network-watcher v1.2.3 (build=abcdef123456)
```

---

## Label reference (summary)

Assuming `NETWORK_WATCHER_PREFIX=network-watcher`:

| Label                                | Scope     | Type   | Description                                                                                |
| ------------------------------------ | --------- | ------ | ------------------------------------------------------------------------------------------ |
| `network-watcher.<network>`          | Container | bool   | Attach/detach the container to/from Docker network `<network>`.                            |
| `network-watcher.<network>.internal` | Container | bool   | Desired `internal` flag for network `<network>` (managed network-level preference).        |
| `network-watcher.alias`              | Container | string | Network alias used when attaching this container.                                          |
| `network-watcher.detachdefault`      | Container | bool   | Per-container override for detach-other: `false` disables detach-other for that container. |

If a container has **no label** starting with `network-watcher.` (or your prefix), it is completely ignored by the watcher.

---

## Example: watcher with docker-socket-proxy

```yaml
version: "3.9"

services:
  socket-proxy:
    image: tecnativa/docker-socket-proxy
    container_name: socket-proxy
    environment:
      CONTAINERS: 1
      NETWORKS: 1
      EVENTS: 1
      # add other sections if needed (e.g. INFO, PING, VERSION)
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
    networks:
      - traefikfront
    restart: unless-stopped

  network-watcher:
    image: ghcr.io/<your-org>/<your-repo>:latest
    container_name: network-watcher
    environment:
      DOCKER_HOST: http://socket-proxy:2375
      NETWORK_WATCHER_PREFIX: network-watcher
      NETWORK_WATCHER_DETACH_OTHER: "true"
      NETWORK_WATCHER_PRUNE_UNUSED_NETWORKS: "true"
      NETWORK_WATCHER_PRUNE_ORPHAN_NETWORKS: "true"
      NETWORK_WATCHER_PRUNE_ORPHAN_MIN_AGE: "60"
      NETWORK_WATCHER_PRUNE_ORPHAN_INCLUDE_COMPOSE: "false"
      RESCAN_SECONDS: "30"
    restart: unless-stopped
    networks:
      - traefikfront

networks:
  traefikfront:
    external: true
```

Example service using it:

```yaml
services:
  homepage:
    image: ghcr.io/gethomepage/homepage:latest
    container_name: homepage
    environment:
      PUID: 107
      PGID: 108
      HOMEPAGE_ALLOWED_HOSTS: 10.248.11.3:3000,cerede.eu
    volumes:
      - ${DATA_PATH}/homepage:/app/config
      - ./docker.yml:/app/config/docker.yaml
      - ./settings.yml:/app/config/settings.yaml
      - ./widgets.yml:/app/config/widgets.yaml
      - ./services.yml:/app/config/services.yaml
      - ./bookmarks.yml:/app/config/bookmarks.yaml
    restart: unless-stopped
    labels:
      network-watcher.traefikfront: "true"
      network-watcher.socketproxy: "true"
      # network-watcher.detachdefault: "false"  # opt out of detach-other for this container if needed
```

---

## Sample compose

A fuller sample compose for the watcher can be found in:

[➡️ Sample compose](./compose.yml)

Use it as a reference for wiring the watcher with your existing networks, traefik, and socket-proxy.

---

## Notes & limitations

* This watcher **only** uses Docker’s HTTP API; direct `unix://` sockets are intentionally not supported.
* It is designed to run **behind a socket proxy** with a reduced permission surface.
* Orphan pruning is conservative by default (min age, skip Compose networks) and can be tightened or relaxed depending on your environment.
* Changing the `internal` flag of networks will cause a brief disconnect/reconnect of containers on that network; use with awareness in production.

Happy hacking and enjoy declarative network wiring via labels 🚀
