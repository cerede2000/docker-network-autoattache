# Docker Network Watcher

A small container that **watches Docker events** and **attaches/detaches networks to containers based on labels**.

It talks to the Docker Engine **over HTTP** (typically through a `docker-socket-proxy`), so you don’t need to mount `/var/run/docker.sock` directly inside the watcher container.

Designed for Docker 29+ (API 1.52) but will auto-detect the API version at runtime.

---

## What it does

- Watches Docker **container events** (`create`, `start`, `destroy`, etc.) and an optional **periodic rescan**.
- Looks for labels with a configurable **prefix** (default: `network-watcher.*`).
- For each container with matching labels:
  - **Attaches** it to the requested networks.
  - **Detaches** it from networks that were previously managed when labels are disabled/removed.
  - **Optionally detaches all other networks** (opt-in via env) so labels fully define the container’s connectivity.
- **Creates networks on demand** if they don’t exist.
- Can **enforce `internal` / non-internal** for labelled networks, recreating the network if needed.
- Optionally **prunes unused and orphan networks** in a **safe way** (never deleting networks still referenced by containers).

The watcher **never touches containers that don’t have any label with the configured prefix**.

---

## How it works (high level)

1. At startup:
   - Detects the Docker API version from `/version` (fallback: `v1.52`).
   - Connects to Docker Engine via HTTP (from `DOCKER_HOST`, default `http://socket-proxy:2375`).
   - Runs an **initial reconciliation** of existing containers (optional, configurable).

2. Runtime:
   - Listens to the Docker `/events` stream for container events.
   - Optionally runs a **periodic rescan** of all containers and networks.
   - For each relevant container:
     - Reads its labels (with your prefix, e.g. `network-watcher.*`).
     - Computes which networks should be **attached**, **detached**, and which should be left alone.
     - Ensures required networks exist (and match `internal` preference if specified).
     - Optionally detaches all “other” networks not mentioned in labels.
   - Optionally prunes **unused** networks that were managed for a container that was destroyed.
   - Optionally prunes **orphan** networks (no containers, not referenced by any container).

Everything goes through the Docker HTTP API: listing containers, inspecting, connecting/disconnecting, creating/removing networks, and streaming events.

---

## Label model

The label prefix is configurable via `NETWORK_WATCHER_PREFIX` (default `network-watcher`).

For the examples below, we assume:

```env
NETWORK_WATCHER_PREFIX=network-watcher
````

### 1. Attach/detach networks

```yaml
labels:
  network-watcher.traefikfront: "true"
  network-watcher.socketproxy: "true"
  network-watcher.psu: "false"
```

For this container:

* `network-watcher.traefikfront: true`
  → container must be attached to Docker network `traefikfront`.

* `network-watcher.socketproxy: true`
  → container must be attached to Docker network `socketproxy`.

* `network-watcher.psu: false`
  → network `psu` is considered **managed** for this container, but **not desired**.
  If the container was previously attached to `psu` (by labels), the watcher will disconnect it (when `AUTO_DISCONNECT=true`).

* Any networks **not mentioned** in `network-watcher.*` labels are left alone, **unless** you enable the global “detach other networks” mode (see below).

> A container is only managed by the watcher if it has at least **one** label starting with `network-watcher.` (or your custom prefix).

---

### 2. Internal networks

You can ask the watcher to create/maintain a network as **internal** via:

```yaml
labels:
  network-watcher.psu: "true"
  network-watcher.psu.internal: "true"
```

Behaviour:

* If the network `psu` does **not** exist:

  * It is created with:

    * name: `psu`
    * driver: `NETWORK_WATCHER_DEFAULT_DRIVER` (default: `bridge`)
    * `internal: true`

* If `psu` already exists:

  * The watcher checks its current `Internal` flag.
  * If the flag differs from the desired value (`true` here), the watcher:

    * Detaches all containers from `psu` (best effort).
    * Removes the `psu` network.
    * Recreates it with the same driver and `Internal=true`.
    * Reattaches the previously attached containers (without custom aliases, just basic connect).

You can also force a network to become **non-internal**:

```yaml
labels:
  network-watcher.psu: "true"
  network-watcher.psu.internal: "false"
```

If you omit `.internal` for a network, the watcher simply does not enforce the `internal` flag (only creates it non-internal if it doesn’t exist and is needed).

---

### 3. Alias label

You can override the alias used when attaching the container to a network:

```yaml
labels:
  network-watcher.traefikfront: "true"
  network-watcher.alias: "my-service"
```

In that case, when the watcher attaches the container to `traefikfront`, the Docker endpoint alias will be `my-service` (instead of the container name).

By default (no alias label), the alias = container name.

---

### 4. Detach other networks (global mode)

Sometimes you want the watcher to **fully control** the networks of the container, i.e.:

> “If it’s not in labels, I don’t want this container attached to it.”

You can enable this globally with:

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

1. Attach the container to `traefikfront` and `socketproxy`.
2. Detach it from **all other non-system networks**:

   * i.e. networks that:

     * are currently attached to the container,
     * are not in `network-watcher.*` labels,
     * and are not system networks (`bridge`, `host`, `none`, `ingress`, `docker_gwbridge`).

> The watcher will **never** detach from `host`, `bridge`, `none`, `ingress`, `docker_gwbridge`.

#### Per-container override: `detachdefault`

If you enable `NETWORK_WATCHER_DETACH_OTHER=true` globally but you want **a specific container** to keep its other networks, use:

```yaml
labels:
  network-watcher.detachdefault: "false"
```

For that container:

* Detach-other mode is **disabled**, even if the global env is `true`.
* The watcher still attaches/detaches networks based on labels, but does not remove any “other” networks.

---

## Network pruning

The watcher has two separate pruning mechanisms, both **opt-in** and **safe by design**.

### 1. Prune unused networks (per-container)

**Env:** `NETWORK_WATCHER_PRUNE_UNUSED_NETWORKS` (default: `true`)

When a container is **destroyed** (`event: destroy`), the watcher:

1. Looks at the networks it used to manage for that container.
2. For each such network:

   * Checks if it is a system network (`bridge`, `host`, `none`, `ingress`, `docker_gwbridge`) → if yes, skip.
   * Computes all **referenced networks** for all containers (running or not) via:

     * `HostConfig.NetworkMode`
     * `NetworkSettings.Networks`
   * If the network name is still referenced by any container → skip.
   * If the network still has attached containers in its `Containers` map → skip.
   * Otherwise:

     * Removes the network.
     * Logs: `Removed unused network '<name>'`.

This prevents deleting networks that Docker containers still depend on (even stopped ones).

---

### 2. Prune orphan networks (global)

**Env:** `NETWORK_WATCHER_PRUNE_ORPHAN_NETWORKS` (default: `false`)

During each periodic rescan, if enabled, the watcher:

1. Lists all networks.
2. Computes all networks referenced by any container (like above).
3. For each network:

   * Skips system networks: `bridge`, `host`, `none`, `ingress`, `docker_gwbridge`.
   * If the network name is referenced by any container → skip.
   * If the network has any attached containers → skip.
   * Else:

     * Removes the network.
     * Logs: `Removed orphan network '<name>'`.

So an “orphan network” is one that:

* has no attached containers, **and**
* is not used as `NetworkMode` or in `NetworkSettings.Networks` by any container.

This is safe even if you use Docker Compose or other tooling:
If a network belongs to a stack but there is no container left that references it, it is truly orphan and will be recreated automatically by Compose on next `docker compose up`.

---

## Environment variables

### Docker connection

| Variable      | Default                    | Description                                                                               |
| ------------- | -------------------------- | ----------------------------------------------------------------------------------------- |
| `DOCKER_HOST` | `http://socket-proxy:2375` | HTTP(S) endpoint to reach the Docker Engine. `unix://` is **not supported**. Use a proxy. |

If you use something like `docker-socket-proxy`, this will typically be `http://socket-proxy:2375`.

> If `DOCKER_HOST` starts with `tcp://`, it is normalized to `http://…`.
> If it has no scheme, `http://` is prepended.

---

### Core behaviour

| Variable                      | Default           | Description                                                                  |
| ----------------------------- | ----------------- | ---------------------------------------------------------------------------- |
| `NETWORK_WATCHER_PREFIX`      | `network-watcher` | Prefix for labels. All labels start with `<prefix>.`.                        |
| `NETWORK_WATCHER_ALIAS_LABEL` | `<prefix>.alias`  | Label to override the alias used when attaching networks (per container).    |
| `INITIAL_ATTACH`              | `true`            | If `true`, the watcher reconciles existing containers on startup.            |
| `INITIAL_RUNNING_ONLY`        | `false`           | If `true`, only running containers are processed during initial attach.      |
| `AUTO_DISCONNECT`             | `true`            | If `true`, detach networks when labels are removed or set to a falsey value. |
| `RESCAN_SECONDS`              | `30`              | Interval for periodic rescan. `0` = disable periodic rescan.                 |
| `DEBUG`                       | `false`           | If `true`, enable verbose debug logging.                                     |

---

### Network creation & internal flag

| Variable                         | Default  | Description                                                                                                   |
| -------------------------------- | -------- | ------------------------------------------------------------------------------------------------------------- |
| `NETWORK_WATCHER_DEFAULT_DRIVER` | `bridge` | Driver used when creating a network that does not exist yet (unless we reuse an existing driver on recreate). |

Networks are **created on demand** when a label requires them and they don’t exist, using this driver and the `internal` flag (if specified via labels).

---

### Pruning

| Variable                                | Default | Description                                                                                                                                      |
| --------------------------------------- | ------- | ------------------------------------------------------------------------------------------------------------------------------------------------ |
| `NETWORK_WATCHER_PRUNE_UNUSED_NETWORKS` | `true`  | When a container is destroyed, prune networks that were managed **for this container**, iff they are not referenced/used by any other container. |
| `NETWORK_WATCHER_PRUNE_ORPHAN_NETWORKS` | `false` | During periodic rescan, prune networks that have **no containers** and are not referenced by any container, globally.                            |

Both pruning modes:

* Never delete system networks (`bridge`, `host`, `none`, `ingress`, `docker_gwbridge`).
* Never delete networks that are still referenced by containers (`NetworkMode` or `NetworkSettings.Networks`).

---

### Detach other networks

| Variable                                | Default                  | Description                                                                                                                                                        |
| --------------------------------------- | ------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `NETWORK_WATCHER_DETACH_OTHER`          | `false`                  | If `true`, for containers with watcher labels, detach all **non-system** networks not referenced in labels, unless overridden per container (see `detachdefault`). |
| `NETWORK_WATCHER_DETACH_OVERRIDE_LABEL` | `<prefix>.detachdefault` | Per-container override label. If present and set to `false`, detaching “other” networks is disabled for that container, even if global detach-other is enabled.    |

The watcher also uses the container’s own `HOSTNAME` to detect itself and **never** applies detach-other to its own container.

---

### Version info

| Variable             | Default                   | Description                                        |
| -------------------- | ------------------------- | -------------------------------------------------- |
| `WATCHER_VERSION`    | `dev`                     | Version string displayed at startup.               |
| `WATCHER_BUILD_HASH` | `unknown` or `GIT_COMMIT` | Build hash displayed at startup (e.g. git commit). |

On startup, you’ll see a log like:

```text
Starting docker-network-watcher v1.2.3 (build=abcdef123456)
```

---

## Label reference (summary)

For `NETWORK_WATCHER_PREFIX=network-watcher`:

| Label                                | Scope     | Type   | Description                                                                                           |
| ------------------------------------ | --------- | ------ | ----------------------------------------------------------------------------------------------------- |
| `network-watcher.<network>`          | Container | bool   | Attach/detach this container to/from Docker network `<network>`.                                      |
| `network-watcher.<network>.internal` | Network   | bool   | Desired `internal` flag for Docker network `<network>`.                                               |
| `network-watcher.alias`              | Container | string | Alias to use when connecting the container to networks (instead of the container name).               |
| `network-watcher.detachdefault`      | Container | bool   | Per-container override for detach-other. `false` = do *not* detach other networks for this container. |

If a container has **no** label starting with `network-watcher.`, it is completely ignored by the watcher.

---

## Example: running the watcher

Minimal example with a `docker-socket-proxy` and the watcher:

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
      # (add more switches if needed)
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
      NETWORK_WATCHER_PRUNE_ORPHAN_NETWORKS: "false"
      RESCAN_SECONDS: "30"
    restart: unless-stopped
    networks:
      - traefikfront

networks:
  traefikfront:
    external: true
```

Then, on any container you want the watcher to manage:

```yaml
labels:
  network-watcher.traefikfront: "true"
  network-watcher.socketproxy: "true"
  #network-watcher.detachdefault: "false"  # opt out of global detach-other, if needed
```

---

## Sample compose

A more complete example `docker-compose` file is available here:

[➡️ See the sample compose file](./docker-compose.sample.yml)

Use it as a starting point to integrate the watcher into your environment.

---

## Notes & limitations

* This service only supports **HTTP / HTTPS** access to Docker (`DOCKER_HOST`). It will **not** connect to `unix:///var/run/docker.sock` directly.
* It is designed to be used **behind a socket proxy** for security (e.g. `docker-socket-proxy`).
* It will not modify containers that do **not** have labels with the configured prefix.
* When changing `.internal` flags for an existing network, the watcher will temporarily disconnect/reconnect containers on that network. This is best used in controlled environments where short network blips are acceptable.

---

Happy hacking, and enjoy declarative network wiring via labels 🚀
