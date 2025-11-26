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
from requests.exceptions import RequestException, ReadTimeout, HTTPError

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

    def inspect_container(self, container_id: str) -> dict | None:
        try:
            resp = self._get(f"/containers/{container_id}/json")
            resp.raise_for_status()
            return resp.json()
        except HTTPError as e:
            # 404 => conteneur déjà disparu, cas normal sur certains events (die/destroy)
            if e.response is not None and e.response.status_code == 404:
                return None
            raise

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
        /networks?filters={"name":["name"]} est fuzzy, donc on filtre ensuite
        sur Name/Id exacts.
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
        Inspect par nom/ID exact : on résout via _find_networks_by_name,
        puis /networks/{id} pour avoir Containers.
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
        Suppression en résolvant nom/ID -> Id, puis DELETE /networks/{id}.
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

        # SPECIAL: detachdefault est un flag global, pas un réseau
        if rest == "detachdefault":
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


def network_internal_desired(
    api: DockerAPI,
    net_name: str,
    cfg: dict,
) -> bool:
    """True s'il existe au moins un conteneur avec <prefix>.<net>.internal=true."""
    prefix: str = cfg["label_prefix"]
    key = f"{prefix}.{net_name}.internal"

    try:
        containers = api.list_containers(all_containers=True)
    except RequestException as e:
        log(f"[internal-check] Error listing containers for network '{net_name}': {e}")
        return False

    for c in containers:
        labels = c.get("Labels") or {}
        if parse_bool(str(labels.get(key)), False):
            return True
    return False


def reattach_labelled_containers(
    api: DockerAPI,
    cfg: dict,
    net_name: str,
    reason: str,
) -> None:
    """Après création/recréation du réseau, rebrancher tous les conteneurs
    qui ont le label <prefix>.<net_name>: true, s'ils ne sont pas déjà attachés.
    """
    prefix: str = cfg["label_prefix"]
    alias_label: str = cfg["alias_label"]
    debug: bool = cfg["debug"]

    key = f"{prefix}.{net_name}"

    try:
        containers = api.list_containers(all_containers=True)
    except RequestException as e:
        log(f"[{reason}] Error listing containers to reattach for network '{net_name}': {e}")
        return

    for c in containers:
        labels = c.get("Labels") or {}
        if not parse_bool(str(labels.get(key)), False):
            continue

        cid = c.get("Id") or c.get("ID")
        if not isinstance(cid, str):
            continue

        # Vérifier si le conteneur est déjà attaché à ce réseau
        inspect = api.inspect_container(cid)
        if not inspect:
            continue
        nets = inspect.get("NetworkSettings", {}).get("Networks", {}) or {}
        if net_name in nets:
            if debug:
                cname = (inspect.get("Name") or cid).lstrip("/")
                log(
                    f"[{reason}] Container '{cname}' already attached to '{net_name}', "
                    "skipping reattach."
                )
            continue

        name = inspect.get("Name") or cid
        cname = str(name).lstrip("/")
        alias = labels.get(alias_label) or cname

        try:
            api.connect_network(net_name, cid, alias=alias)
            log(f"[{reason}] Reattached labeled container '{cname}' to network '{net_name}'")
        except RequestException as e:
            if debug:
                log(
                    f"[{reason}] Failed to reattach labeled container {cid[:12]} "
                    f"to network '{net_name}': {e}"
                )


def ensure_network_state(
    api: DockerAPI,
    net_name: str,
    cfg: dict,
    reason: str = "ensure",
) -> None:
    """S'assure que le réseau existe et a le bon flag Internal,
    en fonction des labels .internal de tous les conteneurs.

    N'importe quel réseau Docker peut être converti internal <-> non internal.
    """
    debug: bool = cfg["debug"]
    default_network_driver: str = cfg["default_network_driver"]

    internal_needed = network_internal_desired(api, net_name, cfg)

    info = api.inspect_network(net_name)

    # Le réseau n'existe pas -> on le crée avec le bon flag
    if info is None:
        try:
            if debug:
                log(
                    f"[{reason}] Network '{net_name}' does not exist; "
                    f"creating with driver '{default_network_driver}', "
                    f"internal={internal_needed}"
                )
            api.create_network(
                net_name,
                driver=default_network_driver,
                internal=internal_needed,
            )
            log(f"[{reason}] Created network '{net_name}' (internal={internal_needed})")
            # Après création, rebrancher tous les conteneurs qui ont le label pour ce réseau
            reattach_labelled_containers(api, cfg, net_name, reason)
        except RequestException as e:
            log(
                f"[{reason}] Failed to create network '{net_name}' "
                f"(internal={internal_needed}): {e}"
            )
        return

    # Réseau existant
    current_internal = bool(info.get("Internal"))
    driver = info.get("Driver") or default_network_driver

    if current_internal == internal_needed:
        if debug:
            log(
                f"[{reason}] Network '{net_name}' already has "
                f"internal={current_internal}; no change."
            )
        return

    # Conversion internal <-> non internal
    containers = (info.get("Containers") or {}).copy()
    if debug:
        log(
            f"[{reason}] Changing network '{net_name}' internal flag from "
            f"{current_internal} to {internal_needed}; "
            f"{len(containers)} container(s) will be recycled."
        )

    # Détacher tous les conteneurs
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

    # Suppression du réseau
    try:
        api.remove_network(net_name)
        if debug:
            log(
                f"[{reason}] Removed network '{net_name}' "
                f"for recreation with internal={internal_needed}."
            )
    except RequestException as e:
        log(
            f"[{reason}] Failed to remove network '{net_name}' "
            f"for recreation: {e}"
        )
        return

    # Recréation avec le nouveau flag
    try:
        api.create_network(net_name, driver=driver, internal=internal_needed)
        log(
            f"[{reason}] Recreated network '{net_name}' "
            f"(driver={driver}, internal={internal_needed})."
        )
    except RequestException as e:
        log(
            f"[{reason}] Failed to recreate network '{net_name}' "
            f"with internal={internal_needed}: {e}"
        )
        return

    # Rattacher les conteneurs qui étaient déjà sur ce réseau avant recréation
    for cid, cinfo in containers.items():
        if not isinstance(cid, str):
            continue
        try:
            api.connect_network(net_name, cid, alias=None)
            if debug:
                cname = cinfo.get("Name") or cid
                log(
                    f"[{reason}] Reattached container '{cname}' "
                    f"({cid[:12]}) to network '{net_name}'."
                )
        except RequestException as e:
            log(
                f"[{reason}] Failed to reattach container {cid} "
                f"to network '{net_name}': {e}"
            )

    # Et rebrancher tous les conteneurs qui ont le label pour ce réseau
    reattach_labelled_containers(api, cfg, net_name, reason)


def maybe_prune_network(
    api: DockerAPI,
    network: str,
    cfg: dict,
    reason: str = "prune",
) -> None:
    """Prune un réseau s'il n'a plus aucun conteneur attaché.

    S'applique à n'importe quel réseau Docker, pas seulement ceux créés par le watcher.
    """
    if not cfg.get("prune_unused_networks"):
        return

    debug: bool = cfg["debug"]

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
    except RequestException as e:
        if debug:
            log(f"[{reason}] Failed to remove unused network '{network}': {e}")


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
        if attrs is None:
            # Conteneur disparu entre l'event et l'inspect
            if debug:
                log(f"[{reason}] Container {container_id[:12]} not found; skipping.")
            return
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

    detach_default = parse_bool(labels.get(f"{label_prefix}.detachdefault"), False)

    prev_managed = managed_networks.get(container_id, set())
    # Union pour garder mémoire des réseaux déjà gérés (cas label supprimé)
    managed = set(prev_managed) | set(referenced_networks)

    # Réseaux actuellement attachés que l'on considère "gérés"
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
            f"to_disconnect={sorted(to_disconnect)}, "
            f"detach_default={detach_default}",
        )

    # 1) S'assurer de l'existence + internal flag pour les réseaux concernés
    nets_to_ensure = set(desired_networks) | set(internal_networks)
    for net_name in sorted(nets_to_ensure):
        ensure_network_state(
            api,
            net_name,
            cfg,
            reason=reason,
        )

    # 2) Attach des réseaux desired
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

    # 3) Detach des réseaux gérés plus désirés (label false ou supprimé)
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

    # 4) Optionnel : détacher les réseaux "par défaut" de la stack
    #    (tous les réseaux non gérés par label et non-internal)
    if detach_default:
        current_attached = (attached_names - to_disconnect) | to_connect
        for net_name in sorted(current_attached):
            if net_name in desired_networks:
                # réseaux explicitement gérés par label -> on ne les touche pas
                continue

            info = api.inspect_network(net_name)
            if not info:
                continue
            if info.get("Internal"):
                # on ne détache pas un autre réseau internal
                continue

            try:
                if debug:
                    log(
                        f"[{reason}] detachdefault: disconnecting '{name}' "
                        f"from default/non-managed network '{net_name}'",
                    )
                api.disconnect_network(net_name, container_id, force=False)
                log(
                    f"[{reason}] detachdefault: disconnected '{name}' "
                    f"from network '{net_name}'"
                )
                maybe_prune_network(
                    api,
                    net_name,
                    cfg,
                    reason=f"{reason}:prune",
                )
            except RequestException as e:
                log(
                    f"[{reason}] Failed to disconnect '{name}' "
                    f"from default/non-managed network '{net_name}': {e}",
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

                # destroy : nettoyage + prune éventuel
                if status == "destroy":
                    nets = managed_networks.pop(cid, set())
                    if cfg.get("prune_unused_networks") and nets:
                        for net_name in sorted(nets):
                            maybe_prune_network(
                                api,
                                net_name,
                                cfg,
                                reason="event:destroy:prune",
                            )
                    continue

                # Autres statuts : reconcile normal
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
