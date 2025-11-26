# Docker Network Watcher

`docker-network-watcher` is a small sidecar service that automatically attaches and detaches Docker networks to containers based on labels.

It is designed to:

- Work **against the Docker HTTP API** (no direct `/var/run/docker.sock` mount).
- Be used safely behind a **Docker Socket Proxy**.
- Let you declaratively manage container network membership with simple labels like:

```yaml
labels:
  network-watcher.traefik: "true"
  network-watcher.cloudflare: "false"
````

---

## How it works

The watcher connects to the Docker Engine API over HTTP (typically via a socket proxy):

* It discovers the API version dynamically via `/version`.
* It listens to Docker **events** (`create`, `start`, `update`, `stop`, `destroy`, …).
* It periodically rescans containers (optional) to stay in sync.

For each container:

1. It reads labels matching a configurable prefix (default: `network-watcher.`).
2. For each label of the form `<prefix>.<network-name>=<value>`:

   * If the value is truthy (`"true"`, `"1"`, `"yes"`, `"on"`), it **attaches** the container to the Docker network `<network-name>`.
   * If the value is falsy (`"false"`, `"0"`, `"no"`, `"off"`) or the label is removed, it **detaches** the container from `<network-name>` (if `AUTO_DISCONNECT` is enabled).
3. An optional alias label lets you control the network alias of the container on those networks.

The watcher maintains a small in-memory cache per container to handle label removal (i.e. “this label used to exist, now it’s gone → detach”).

> ⚠️ Containers using `network_mode: host` or `network_mode: container:<id>` are ignored (Docker doesn’t allow attaching extra networks in those modes).

---

## Network auto-create and pruning

### Auto-create networks

If a label references a network that does **not** exist yet, the watcher can automatically create it:

```yaml
labels:
  network-watcher.my-custom-net: "true"
```

When reconciling:

* It checks whether the network `my-custom-net` exists.
* If not, it calls the Docker API to **create** it with the configured driver (`NETWORK_WATCHER_NETWORK_DRIVER`, default: `bridge`).
* Then it attaches the container to that newly created network.

This lets you declare networks purely via labels, without having to manually define them up-front in Compose or CLI.

### Auto-prune unused networks

When `AUTO_DISCONNECT` is enabled and all containers managed by the watcher are detached from a given network, you can optionally ask the watcher to automatically **remove** the now-unused network:

* After disconnecting the last managed container from a network, if `NETWORK_WATCHER_PRUNE_UNUSED_NETWORKS=true`:

  * The watcher inspects the network.
  * If **no containers** are attached, it calls Docker to **remove** that network.

This is useful to avoid accumulation of ephemeral networks created only for short-lived containers.

> Note: pruning is best-effort and only happens as part of the watcher’s own attach/detach logic. It does not periodically scan all networks on its own.

---

## Labels

By default, the label prefix is `network-watcher`.
A label key is built as:

```text
<prefix>.<network-name>
```

Example:

```yaml
labels:
  network-watcher.traefik: "true"
  network-watcher.cloudflare: "false"
```

Behavior:

* `network-watcher.traefik: "true"`
  → Ensure the container is attached to the Docker network **`traefik`** (creating it first if necessary).
* `network-watcher.cloudflare: "false"`
  → Ensure the container is **detached** from the Docker network **`cloudflare`**.
* Removing `network-watcher.traefik` entirely
  → The watcher will detect that and **detach** the container from `traefik` (with `AUTO_DISCONNECT=true`).
  If this was the last container on that network and `NETWORK_WATCHER_PRUNE_UNUSED_NETWORKS=true`, the network may also be removed.

### Alias label

You can optionally define a network alias for all managed networks via an alias label:

```yaml
labels:
  network-watcher.traefik: "true"
  network-watcher.alias: "my-service"
```

By default, the alias label key is `<prefix>.alias`, e.g. `network-watcher.alias`.

If not set, the alias falls back to the container name.

---

## Environment variables

| Variable                                | Default                    | Description                                                                                                             |
| --------------------------------------- | -------------------------- | ----------------------------------------------------------------------------------------------------------------------- |
| `DOCKER_HOST`                           | `http://socket-proxy:2375` | URL of the Docker Engine / Socket Proxy (must be HTTP/HTTPS, no `unix://` sockets).                                     |
| `NETWORK_WATCHER_PREFIX`                | `network-watcher`          | Prefix used for labels (e.g. `network-watcher.traefik=true`).                                                           |
| `NETWORK_WATCHER_ALIAS_LABEL`           | `<prefix>.alias`           | Label key used for alias (e.g. `network-watcher.alias`).                                                                |
| `INITIAL_ATTACH`                        | `true`                     | If `true`, reconcile all existing containers at startup.                                                                |
| `INITIAL_RUNNING_ONLY`                  | `false`                    | If `true`, only reconcile containers that are currently running at startup.                                             |
| `AUTO_DISCONNECT`                       | `true`                     | If `true`, detach from networks when label is falsy or removed.                                                         |
| `RESCAN_SECONDS`                        | `30`                       | Periodic full rescan interval in seconds. Set to `0` to disable periodic rescan.                                        |
| `DEBUG`                                 | `false`                    | If `true`, print verbose debug logs about decisions (attach/detach, desired vs actual state, etc.).                     |
| `NETWORK_WATCHER_NETWORK_DRIVER`        | `bridge`                   | Docker network driver to use when **auto-creating** networks (e.g. `bridge`, `overlay`, depending on your environment). |
| `NETWORK_WATCHER_PRUNE_UNUSED_NETWORKS` | `false`                    | If `true`, automatically remove networks that have **no containers** attached after disconnecting the last one.         |

Truthy values: `1`, `true`, `yes`, `y`, `on`
Falsy values: `0`, `false`, `no`, `n`, `off`

---

## Usage

### 1. With Docker Socket Proxy

It is strongly recommended to use a socket proxy instead of exposing `/var/run/docker.sock` directly to the watcher.

The repository provides a sample Docker Compose file that:

* Runs a socket proxy (e.g. `lscr.io/linuxserver/socket-proxy`).
* Runs the `network-watcher` container.
* Connects the watcher to the proxy via `DOCKER_HOST`.

👉 See the sample compose file:
[`docker-compose.sample.yml`](./docker-compose.sample.yml)

### 2. Example label usage

```yaml
services:
  myapp:
    image: nginx:alpine
    labels:
      # Attach myapp to 'traefik' network (auto-created if needed)
      network-watcher.traefik: "true"
      # Make sure it's NOT attached to 'cloudflare'
      network-watcher.cloudflare: "false"
      # Optional: override alias on all managed networks
      network-watcher.alias: "myapp-nginx"
```

You can flip labels at any time and the watcher will attach/detach networks accordingly:

* Change `network-watcher.cloudflare` from `"false"` to `"true"` → the watcher will connect `myapp` to the `cloudflare` network (creating it if needed).
* Remove `network-watcher.traefik` entirely → the watcher will detach `myapp` from `traefik`, and (with `NETWORK_WATCHER_PRUNE_UNUSED_NETWORKS=true`) may remove the `traefik` network if it’s now unused.

---

## Design notes

* Works with Docker 29+ (API version detection via `/version`).
* Uses only the Docker **HTTP API**: no dependency on the Docker CLI inside the container.
* Can **auto-create** networks on demand, with a configurable driver.
* Can **auto-prune** unused networks when the last managed container is detached.
* Stateless by design: all “state” is derived from Docker + a small in-memory cache for label tracking.
* Safe to restart: on startup, the watcher re-evaluates containers according to the current labels.
