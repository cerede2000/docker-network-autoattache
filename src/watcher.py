#!/usr/bin/env python3
import json
import os
import sys
import threading
import time
from datetime import datetime
from typing import Dict, Set, Tuple

import requests
from requests import Response
from requests.exceptions import RequestException, ReadTimeout

# ---------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------


def log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def parse_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    value = value.strip().lower()
    if value in ("1", "true", "yes", "y", "on"):
        return True
    if value in ("0", "false", "no", "n", "off"):
        return False
    return default


# container_id -> set of network names we manage for this container
managed_networks: Dict[str, Set[str]] = {}

# networks that were auto-created by the watcher (eligible for prune)
auto_created_networks: Set[str] = set()

# container_id -> set of networks this container marks as internal (via .internal=true)
container_internal_networks: Dict[str, Set[str]] = {}

# network_name -> number of containers that currently want this network internal
internal_refcounts: Dict[str, int] = {}


# ---------------------------------------------------------
# Docker HTTP client
# ---------------------------------------------------------


class DockerAPI:
    def __init__(self, base_url: str, timeout: int = 5) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.api_prefix = self._detect_api_prefix()

    def _detect_api_prefix(self) -> str:
        url = f"{self.base_url}/version"
        try:
            resp = requests.get(url, timeout=self.timeout)
            resp.raise_for_status()
            data = resp.json()
            api_version = data.get("ApiVersion") or data.get("APIVersion") or "1.52"
            log(f"Detected Docker API version {api_version}")
            return f"/v{api_version}"
        except Exception as e:  # noqa: BLE001
            log(
                f"WARNING: failed to detect Docker API version ({e}); "
                "falling back to v1.52"
            )
            return "/v1.52"

    def _url(self, path: str) -> str:
        if not path.startswith("/"):
            path = "/" + path
        return f"{self.base_url}{self.api_prefix}{path}"

    def _get(
        self,
        path: str,
        params: dict | None = None,
        stream: bool = False,
        timeout: int | None = None,
    ) -> Response:
        url = self._url(path)
        return requests.get(
            url,
            params=params,
            timeout=timeout or self.timeout,
            stream=stream,
        )

    def _post(self, path: str, payload: dict, timeout: int | None = None) -> Response:
        url = self._url(path)
        return requests.post(
            url,
            json=payload,
            timeout=timeout or self.timeout,
        )

    def _delete(self, path: str, timeout: int | None = None) -> Response:
        url = self._url(path)
        return requests.delete(
            url,
            timeout=timeout or self.timeout,
        )

    # ---------- High-level container helpers ----------

    def list_containers(self, all_containers: bool = True) -> list[dict]:
        params = {"all": "1" if all_containers else "0"}
        resp = self._get("/containers/json", params=params)
        resp.raise_for_status()
        return resp.json()

    def inspect_container(self, container_id: str) -> dict:
        resp = self._get(f"/containers/{container_id}/json")
        resp.raise_for_status()
        return resp.json()

    def connect_network(
        self,
        network: str,
        container_id: str,
        alias: str | None = None,
    ) -> None:
        payload: dict = {"Container": container_id}
        if alias:
            payload["EndpointConfig"] = {"Aliases": [alias]}
        resp = self._post(f"/networks/{network}/connect", payload)
        resp.raise_for_status()

    def disconnect_network(
        self,
        network: str,
        container_id: str,
        force: bool = False,
    ) -> None:
        payload: dict = {"Container": container_id, "Force": force}
        resp = self._post(f"/networks/{network}/disconnect", payload)
        resp.raise_for_status()

    # ---------- Network helpers ----------

    def _find_networks_by_name(self, name: str) -> list[dict]:
        """
        Use /networks?filters={"name":["name"]} then filter on exact match
        on Name or Id to avoid partial matches (Docker 'name' filter is fuzzy).
        """
        params = {"filters": json.dumps({"name": [name]})}
        resp = self._get("/networks", params=params)
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, list):
            return []

        result: list[dict] = []
        for net in data:
            n_name = net.get("Name")
            n_id = net.get("Id") or net.get("ID")
            if n_name == name or n_id == name:
                result.append(net)
        return result

    def network_exists(self, network: str) -> bool:
        try:
            nets = self._find_networks_by_name(network)
        except RequestException:
            return False
        return len(nets) > 0

    def create_network(
        self,
        name: str,
        driver: str | None = None,
        internal: bool | None = None,
    ) -> dict:
        payload: dict = {
            "Name": name,
            "CheckDuplicate": True,
        }
        if driver:
            payload["Driver"] = driver
        if internal is not None:
            payload["Internal"] = bool(internal)
        resp = self._post("/networks/create", payload)
        resp.raise_for_status()
        return resp.json()

    def inspect_network(self, network: str) -> dict | None:
        """
        Inspect by exact name/ID: resolve via _find_networks_by_name, then
        call /networks/{id} to get full details including Containers.
        """
        try:
            nets = self._find_networks_by_name(network)
        except RequestException:
            return None
        if not nets:
            return None
        net_id = nets[0].get("Id") or nets[0].get("ID")
        if not isinstance(net_id, str):
            return None
        try:
            resp = self._get(f"/networks/{net_id}")
            resp.raise_for_status()
        except RequestException:
            return None
        return resp.json()

    def remove_network(self, network: str) -> None:
        """
        Remove by resolving exact name/ID -> Id, then DELETE /networks/{id}.
        """
        nets = self._find_networks_by_name(network)
        if not nets:
            return
        net_id = nets[0].get("Id") or nets[0].get("ID")
        if not isinstance(net_id, str):
            return
        resp = self._delete(f"/networks/{net_id}")
        resp.raise_for_status()

    # ---------- Events ----------

    def events_stream(self, filters: dict | None = None):
        params = {}
        if filters:
            params["filters"] = json.dumps(filters)
        resp = self._get("/events", params=params, stream=True, timeout=60)
        resp.raise_for_status()
        for line in resp.iter_lines(decode_unicode=True):
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            yield event


# ---------------------------------------------------------
# Config loading
# ---------------------------------------------------------


def load_config() -> dict:
    label_prefix = os.getenv("NETWORK_WATCHER_PREFIX", "network-watcher").strip()
    alias_label = os.getenv(
        "NETWORK_WATCHER_ALIAS_LABEL",
        f"{label_prefix}.alias",
    )

    initial_attach = parse_bool(os.getenv("INITIAL_ATTACH", "true"), True)
    initial_running_only = parse_bool(
        os.getenv("INITIAL_RUNNING_ONLY", "false"),
        False,
    )
    auto_disconnect = parse_bool(os.getenv("AUTO_DISCONNECT", "true"), True)
    rescan_seconds_str = os.getenv("RESCAN_SECONDS", "30")

    try:
        rescan_seconds = int(rescan_seconds_str)
        if rescan_seconds < 0:
            rescan_seconds = 0
    except ValueError:
        rescan_seconds = 30

    debug = parse_bool(os.getenv("DEBUG", "false"), False)

    default_network_driver = os.getenv(
        "NETWORK_WATCHER_NETWORK_DRIVER",
        "bridge",
    ).strip() or "bridge"

    prune_unused_networks = parse_bool(
        os.getenv("NETWORK_WATCHER_PRUNE_UNUSED_NETWORKS", "false"),
        False,
    )

    log("Loaded network-watcher configuration:")
    log(f" Label prefix: {label_prefix}")
    log(f" Alias label: {alias_label}")
    log(f" Initial attach: {initial_attach} (running only: {initial_running_only})")
    log(f" Auto-disconnect: {auto_disconnect}")
    log(f" Periodic rescan seconds: {rescan_seconds} (0 = disabled)")
    log(f" Debug: {debug}")
    log(f" Default network driver: {default_network_driver}")
    log(f" Prune unused networks: {prune_unused_networks}")

    return {
        "label_prefix": label_prefix,
        "alias_label": alias_label,
        "initial_attach": initial_attach,
        "initial_running_only": initial_running_only,
        "auto_disconnect": auto_disconnect,
        "rescan_seconds": rescan_seconds,
        "debug": debug,
        "default_network_driver": default_network_driver,
        "prune_unused_networks": prune_unused_networks,
    }


# ---------------------------------------------------------
# Core logic
# ---------------------------------------------------------


def get_base_url_from_env() -> str:
    host = os.getenv("DOCKER_HOST", "http://socket-proxy:2375").strip()

    # Normalize some common values
    if host.startswith("unix://"):
        log(
            "ERROR: unix:// DOCKER_HOST is not supported by this watcher "
            "(use a TCP SocketProxy instead)."
        )
        sys.exit(1)

    if host.startswith("tcp://"):
        host = "http://" + host[len("tcp://") :]

    if not host.startswith("http://") and not host.startswith("https://"):
        host = "http://" + host

    return host.rstrip("/")


def get_container_name(attrs: dict) -> str:
    name = attrs.get("Name")
    if isinstance(name, str) and name:
        return name.lstrip("/")
    names = attrs.get("Names")
    if isinstance(names, list) and names:
        return str(names[0]).lstrip("/")
    cid = attrs.get("Id") or attrs.get("ID")
    if isinstance(cid, str) and cid:
        return cid[:12]
    return "<unknown>"


def get_network_mode(attrs: dict) -> str | None:
    host_cfg = attrs.get("HostConfig") or {}
    mode = host_cfg.get("NetworkMode")
    if isinstance(mode, str):
        return mode
    return None


def extract_desired_networks(
    labels: dict,
    label_prefix: str,
) -> Tuple[Set[str], Set[str], Set[str]]:
    """Return (desired_networks, referenced_networks, internal_networks).

    desired_networks -> networks where label value is truthy
                        for labels of the form <prefix>.<net>=<value>
    referenced_networks -> networks that have a label with this prefix
                           (excluding the .internal ones)
    internal_networks -> networks with label <prefix>.<net>.internal=true
    """
    desired: Set[str] = set()
    referenced: Set[str] = set()
    internal: Set[str] = set()

    prefix = f"{label_prefix}."
    for key, value in labels.items():
        if not isinstance(key, str):
            continue
        if not key.startswith(prefix):
            continue

        rest = key[len(prefix) :]
        if not rest:
            continue

        # Internal flag labels: <prefix>.<net>.internal
        if rest.endswith(".internal"):
            net_name = rest[: -len(".internal")]
            if not net_name:
                continue
            if parse_bool(str(value), False):
                internal.add(net_name)
            continue

        # Regular attach/detach labels: <prefix>.<net>
        net_name = rest
        referenced.add(net_name)
        if parse_bool(str(value), False):
            desired.add(net_name)

    return desired, referenced, internal


def maybe_prune_network(
    api: DockerAPI,
    network: str,
    cfg: dict,
    reason: str = "prune",
) -> None:
    if not cfg.get("prune_unused_networks"):
        return

    debug: bool = cfg["debug"]

    # Only prune networks that were auto-created by the watcher
    if network not in auto_created_networks:
        if debug:
            log(
                f"[{reason}] Not pruning network '{network}' "
                f"(not auto-created by watcher)."
            )
        return

    info = api.inspect_network(network)
    if info is None:
        if debug:
            log(f"[{reason}] Network '{network}' not found when trying to prune.")
        return

    containers = info.get("Containers") or {}
    if containers:
        if debug:
            log(
                f"[{reason}] Network '{network}' not pruned: "
                f"{len(containers)} container(s) still attached.",
            )
        return

    try:
        api.remove_network(network)
        log(f"[{reason}] Removed unused network '{network}'")
        auto_created_networks.discard(network)
    except RequestException as e:
        log(f"[{reason}] Failed to remove unused network '{network}': {e}")


def ensure_network(
    api: DockerAPI,
    net_name: str,
    cfg: dict,
    internal_requested: bool,
    reason: str = "ensure",
) -> None:
    """Ensure that a given network exists, and if internal_requested=True,
    convert it to internal if needed. Demotion to non-internal is handled
    separately via maybe_demote_internal_network when refcount drops to zero.
    """
    debug: bool = cfg["debug"]
    default_network_driver: str = cfg["default_network_driver"]

    info = api.inspect_network(net_name)

    # Network doesn't exist -> create it
    if info is None:
        try:
            if debug:
                log(
                    f"[{reason}] Network '{net_name}' does not exist; "
                    f"creating with driver '{default_network_driver}', "
                    f"internal={internal_requested}"
                )
            api.create_network(
                net_name,
                driver=default_network_driver,
                internal=internal_requested,
            )
            log(f"[{reason}] Created network '{net_name}'")
            auto_created_networks.add(net_name)
        except RequestException as e:
            log(
                f"[{reason}] Failed to create network '{net_name}' "
                f"(internal={internal_requested}): {e}"
            )
        return

    # Network exists
    current_internal = bool(info.get("Internal"))

    # Promotion: non-internal -> internal
    if internal_requested and not current_internal:
        containers = (info.get("Containers") or {}).copy()
        if debug:
            log(
                f"[{reason}] Network '{net_name}' exists but is not internal; "
                f"recreating as internal and reattaching {len(containers)} container(s)."
            )

        # Detach all containers
        for cid, cinfo in containers.items():
            if not isinstance(cid, str):
                continue
            try:
                api.disconnect_network(net_name, cid, force=True)
                if debug:
                    cname = cinfo.get("Name") or cid
                    log(
                        f"[{reason}] Disconnected container '{cname}' "
                        f"({cid[:12]}) from '{net_name}' before recreation."
                    )
            except RequestException as e:
                log(
                    f"[{reason}] Failed to disconnect container {cid} "
                    f"from '{net_name}' before recreation: {e}"
                )

        # Remove network
        try:
            api.remove_network(net_name)
            if debug:
                log(
                    f"[{reason}] Removed network '{net_name}' "
                    f"for recreation as internal."
                )
        except RequestException as e:
            log(
                f"[{reason}] Failed to remove network '{net_name}' "
                f"for recreation as internal: {e}"
            )
            return

        # Recreate network as internal, using same driver if possible
        driver = info.get("Driver") or default_network_driver
        try:
            api.create_network(net_name, driver=driver, internal=True)
            log(
                f"[{reason}] Recreated network '{net_name}' as internal "
                f"(driver={driver})."
            )
        except RequestException as e:
            log(
                f"[{reason}] Failed to recreate network '{net_name}' "
                f"as internal: {e}"
            )
            return

        # Reattach previously connected containers
        for cid, cinfo in containers.items():
            if not isinstance(cid, str):
                continue
            try:
                api.connect_network(net_name, cid, alias=None)
                if debug:
                    cname = cinfo.get("Name") or cid
                    log(
                        f"[{reason}] Reattached container '{cname}' "
                        f"({cid[:12]}) to internal network '{net_name}'."
                    )
            except RequestException as e:
                log(
                    f"[{reason}] Failed to reattach container {cid} "
                    f"to internal network '{net_name}': {e}"
                )
    else:
        # Either internal_requested is False, or network already matches the desired internal flag.
        if debug:
            log(
                f"[{reason}] Network '{net_name}' already exists "
                f"(internal={current_internal}); no change requested."
            )


def maybe_demote_internal_network(
    api: DockerAPI,
    net_name: str,
    cfg: dict,
    reason: str = "internal-demote",
) -> None:
    """Demote an internal network back to a normal one when no container
    requires it to be internal anymore (refcount reached zero).
    """
    debug: bool = cfg["debug"]
    default_network_driver: str = cfg["default_network_driver"]

    info = api.inspect_network(net_name)
    if info is None:
        if debug:
            log(f"[{reason}] Network '{net_name}' not found for demotion.")
        return

    current_internal = bool(info.get("Internal"))
    if not current_internal:
        if debug:
            log(
                f"[{reason}] Network '{net_name}' is already non-internal; "
                "no demotion needed."
            )
        return

    containers = (info.get("Containers") or {}).copy()
    if debug:
        log(
            f"[{reason}] Demoting network '{net_name}' from internal to normal; "
            f"{len(containers)} container(s) will be reattached."
        )

    # Detach containers
    for cid, cinfo in containers.items():
        if not isinstance(cid, str):
            continue
        try:
            api.disconnect_network(net_name, cid, force=True)
            if debug:
                cname = cinfo.get("Name") or cid
                log(
                    f"[{reason}] Disconnected container '{cname}' "
                    f"({cid[:12]}) from '{net_name}' before demotion."
                )
        except RequestException as e:
            log(
                f"[{reason}] Failed to disconnect container {cid} "
                f"from '{net_name}' before demotion: {e}"
            )

    # Remove network
    try:
        api.remove_network(net_name)
        if debug:
            log(
                f"[{reason}] Removed network '{net_name}' "
                f"for recreation as non-internal."
            )
    except RequestException as e:
        log(
            f"[{reason}] Failed to remove network '{net_name}' "
            f"for recreation as non-internal: {e}"
        )
        return

    # Recreate network as non-internal
    driver = info.get("Driver") or default_network_driver
    try:
        api.create_network(net_name, driver=driver, internal=False)
        log(
            f"[{reason}] Recreated network '{net_name}' as non-internal "
            f"(driver={driver})."
        )
    except RequestException as e:
        log(
            f"[{reason}] Failed to recreate network '{net_name}' "
            f"as non-internal: {e}"
        )
        return

    # Reattach containers
    for cid, cinfo in containers.items():
        if not isinstance(cid, str):
            continue
        try:
            api.connect_network(net_name, cid, alias=None)
            if debug:
                cname = cinfo.get("Name") or cid
                log(
                    f"[{reason}] Reattached container '{cname}' "
                    f"({cid[:12]}) to non-internal network '{net_name}'."
                )
        except RequestException as e:
            log(
                f"[{reason}] Failed to reattach container {cid} "
                f"to non-internal network '{net_name}': {e}"
            )


def update_internal_tracking(
    container_id: str,
    new_internal: Set[str],
    api: DockerAPI,
    cfg: dict,
    reason: str,
) -> None:
    """Track which networks this container marks as internal, update global
    refcounts, and trigger demotion when the last container drops .internal.
    """
    prev_internal = container_internal_networks.get(container_id, set())
    if prev_internal == new_internal:
        return

    debug: bool = cfg["debug"]

    added = new_internal - prev_internal
    removed = prev_internal - new_internal

    # Increment refcounts for newly internal networks
    for net_name in added:
        old = internal_refcounts.get(net_name, 0)
        internal_refcounts[net_name] = old + 1
        if debug:
            log(
                f"[{reason}] Container {container_id[:12]}: "
                f"internal +1 for '{net_name}' -> {internal_refcounts[net_name]}"
            )

    # Decrement refcounts for networks no longer marked internal
    for net_name in removed:
        old = internal_refcounts.get(net_name, 0)
        new_val = max(0, old - 1)
        if new_val == 0:
            internal_refcounts.pop(net_name, None)
            if debug:
                log(
                    f"[{reason}] Container {container_id[:12]}: "
                    f"internal count for '{net_name}' dropped to 0; "
                    "demoting network."
                )
            maybe_demote_internal_network(
                api,
                net_name,
                cfg,
                reason=f"{reason}:internal-demote",
            )
        else:
            internal_refcounts[net_name] = new_val
            if debug:
                log(
                    f"[{reason}] Container {container_id[:12]}: "
                    f"internal -1 for '{net_name}' -> {new_val}"
                )

    # Update per-container tracking
    if new_internal:
        container_internal_networks[container_id] = set(new_internal)
    elif container_id in container_internal_networks:
        container_internal_networks.pop(container_id, None)


def reconcile_container(
    api: DockerAPI,
    container_id: str,
    cfg: dict,
    reason: str = "event",
) -> None:
    label_prefix: str = cfg["label_prefix"]
    alias_label: str = cfg["alias_label"]
    auto_disconnect: bool = cfg["auto_disconnect"]
    debug: bool = cfg["debug"]

    try:
        attrs = api.inspect_container(container_id)
    except RequestException as e:
        log(f"[{reason}] Error inspecting container {container_id}: {e}")
        return

    name = get_container_name(attrs)

    net_mode = get_network_mode(attrs)
    if net_mode and (net_mode == "host" or net_mode.startswith("container:")):
        if debug:
            log(
                f"[{reason}] Skipping '{name}': network_mode={net_mode} "
                "— cannot attach/detach networks."
            )
        return

    labels = attrs.get("Config", {}).get("Labels", {}) or {}
    networks = attrs.get("NetworkSettings", {}).get("Networks", {}) or {}

    desired_networks, referenced_networks, internal_networks = extract_desired_networks(
        labels,
        label_prefix,
    )

    # Update internal refcounts and maybe demote networks if necessary
    update_internal_tracking(
        container_id,
        internal_networks,
        api,
        cfg,
        reason=reason,
    )

    prev_managed = managed_networks.get(container_id, set())
    # Union so we remember networks that were referenced in the past for
    # label-removal handling.
    managed = set(prev_managed) | set(referenced_networks)

    # Networks currently attached that we consider "managed" for this container
    attached_names = set(networks.keys())
    managed_attached = attached_names & managed

    to_connect = desired_networks - managed_attached
    to_disconnect = managed_attached - desired_networks

    alias = labels.get(alias_label) or name

    if debug:
        log(
            f"[{reason}] {name}: desired={sorted(desired_networks)}, "
            f"internal={sorted(internal_networks)}, "
            f"managed_attached={sorted(managed_attached)}, "
            f"to_connect={sorted(to_connect)}, "
            f"to_disconnect={sorted(to_disconnect)}",
        )

    # Ensure networks exist and have the correct "internal" property when requested
    nets_to_ensure = set(desired_networks) | set(internal_networks)
    for net_name in sorted(nets_to_ensure):
        internal_requested = net_name in internal_networks
        ensure_network(
            api,
            net_name,
            cfg,
            internal_requested=internal_requested,
            reason=reason,
        )

    # Attach networks whose label value is truthy
    for net_name in sorted(to_connect):
        try:
            if debug:
                log(
                    f"[{reason}] Connecting '{name}' to '{net_name}' "
                    f"with alias '{alias}'"
                )
            api.connect_network(net_name, container_id, alias=alias)
            log(f"Connecting '{name}' to network '{net_name}'")
            managed.add(net_name)
        except RequestException as e:
            log(
                f"[{reason}] Failed to connect '{name}' to '{net_name}': {e}",
            )

    # Detach networks that are managed but no longer desired (label false or removed)
    if auto_disconnect:
        for net_name in sorted(to_disconnect):
            try:
                if debug:
                    log(
                        f"[{reason}] Disconnecting '{name}' "
                        f"from '{net_name}'",
                    )
                api.disconnect_network(net_name, container_id, force=False)
                log(f"Disconnecting '{name}' from network '{net_name}'")
                managed.discard(net_name)
                maybe_prune_network(
                    api,
                    net_name,
                    cfg,
                    reason=f"{reason}:prune",
                )
            except RequestException as e:
                log(
                    f"[{reason}] Failed to disconnect '{name}' "
                    f"from '{net_name}': {e}",
                )
    elif debug and to_disconnect:
        log(
            f"[{reason}] auto_disconnect disabled; "
            f"would disconnect from: {sorted(to_disconnect)}",
        )

    if managed:
        managed_networks[container_id] = managed
    elif container_id in managed_networks:
        managed_networks.pop(container_id, None)


def initial_attach_all(api: DockerAPI, cfg: dict) -> None:
    if not cfg["initial_attach"]:
        log("Initial attach disabled by configuration.")
        return

    running_only = cfg["initial_running_only"]
    log(
        f"Running initial attach (containers: "
        f"{'running only' if running_only else 'all'})...",
    )
    try:
        containers = api.list_containers(all_containers=not running_only)
    except RequestException as e:
        log(f"Error listing containers during initial attach: {e}")
        return

    if cfg["debug"]:
        log(f"Initial attach: found {len(containers)} container(s).")

    for c in containers:
        cid = c.get("Id") or c.get("ID")
        if not isinstance(cid, str):
            continue
        reconcile_container(api, cid, cfg, reason="initial")

    log("Initial attach complete.")


# ---------------------------------------------------------
# Event loop + periodic rescan
# ---------------------------------------------------------


def event_loop(api: DockerAPI, cfg: dict) -> None:
    relevant_statuses = {
        "create",
        "start",
        "restart",
        "die",
        "stop",
        "destroy",
        "update",
        "rename",
    }
    debug: bool = cfg["debug"]
    filters = {"type": ["container"]}

    log("Event loop started.")
    while True:
        try:
            for event in api.events_stream(filters=filters):
                if event.get("Type") != "container":
                    continue

                status = event.get("status") or event.get("Action")
                if status not in relevant_statuses:
                    continue

                cid = event.get("id") or event.get("Actor", {}).get("ID")
                if not isinstance(cid, str):
                    continue

                if debug:
                    name = event.get("Actor", {}).get("Attributes", {}).get("name")
                    log(f"[event] Processing {status} for {name or cid}")

                # Special case: destroy
                if status == "destroy":
                    # Réseaux gérés pour ce conteneur (pour le prune éventuel)
                    nets = managed_networks.pop(cid, set())

                    # On met à jour le tracking internal, MAIS SANS démotion.
                    # La démotion internal->normal est gérée uniquement lors
                    # des reconcile sur des conteneurs encore existants
                    # (changement de labels), pas sur destroy.
                    internal_nets = container_internal_networks.pop(cid, set())
                    for net_name in sorted(internal_nets):
                        old = internal_refcounts.get(net_name, 0)
                        new_val = max(0, old - 1)
                        if new_val == 0:
                            internal_refcounts.pop(net_name, None)
                            if debug:
                                log(
                                    f"[event:destroy] Container {cid[:12]}: "
                                    f"internal count for '{net_name}' dropped to 0."
                                )
                            # Pas de demote ici : si le réseau est encore utilisé,
                            # il sera ajusté lors d'un futur reconcile. S'il est vide
                            # et auto-créé, le prune ci-dessous se chargera de le supprimer.
                        else:
                            internal_refcounts[net_name] = new_val
                            if debug:
                                log(
                                    f"[event:destroy] Container {cid[:12]}: "
                                    f"internal -1 for '{net_name}' -> {new_val}"
                                )

                    # Prune auto-created networks si plus utilisés
                    if cfg.get("prune_unused_networks") and nets:
                        for net_name in sorted(nets):
                            maybe_prune_network(
                                api,
                                net_name,
                                cfg,
                                reason="event:destroy:prune",
                            )

                    # Pas de reconcile sur un conteneur détruit
                    continue

                # Pour les autres statuts (die, stop, start, update, ...),
                # reconciliation normale.
                reconcile_container(api, cid, cfg, reason=f"event:{status}")
        except ReadTimeout:
            if debug:
                log("Event stream read timeout; reopening...")
            continue
        except RequestException as e:
            log(f"Error in event loop HTTP call: {e}")
        except Exception as e:  # noqa: BLE001
            log(f"Unexpected error in event loop: {e}")
        log("Re-establishing Docker event stream in 5 seconds...")
        time.sleep(5)

def periodic_rescan_loop(api: DockerAPI, cfg: dict) -> None:
    interval = cfg["rescan_seconds"]
    debug: bool = cfg["debug"]

    if interval <= 0:
        log("Periodic rescan disabled.")
        return

    log(f"Periodic rescan thread started (interval {interval} seconds).")

    while True:
        time.sleep(interval)
        if debug:
            log("Periodic rescan: scanning containers.")
        try:
            containers = api.list_containers(all_containers=True)
        except RequestException as e:
            log(f"Error listing containers for rescan: {e}")
            continue

        for c in containers:
            cid = c.get("Id") or c.get("ID")
            if not isinstance(cid, str):
                continue
            reconcile_container(api, cid, cfg, reason="rescan")


# ---------------------------------------------------------
# Main entry
# ---------------------------------------------------------


def main() -> None:
    build_hash = os.getenv("WATCHER_BUILD_HASH", "unknown")
    log(f"Starting docker-network-watcher (build={build_hash})")

    base_url = get_base_url_from_env()
    log(f"Connecting to Docker Engine via HTTP at: {base_url}")

    try:
        api = DockerAPI(base_url=base_url)
    except Exception as e:  # noqa: BLE001
        log(f"Failed to create Docker HTTP client: {e}")
        sys.exit(1)

    cfg = load_config()

    # Initial reconciliation of existing containers
    initial_attach_all(api, cfg)

    # Periodic rescan thread
    if cfg["rescan_seconds"] > 0:
        t = threading.Thread(
            target=periodic_rescan_loop,
            args=(api, cfg),
            daemon=True,
        )
        t.start()

    # Event loop (blocking)
    event_loop(api, cfg)


if __name__ == "__main__":
    main()
