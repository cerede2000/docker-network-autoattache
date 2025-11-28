#!/usr/bin/env python3
import json
import os
import sys
import threading
import time
from datetime import datetime
from typing import Dict, Set, Tuple, Optional, List, Any
from urllib.parse import quote

import requests
from requests import Response
from requests.exceptions import RequestException, ReadTimeout

# Optional: unix socket support
try:
    import requests_unixsocket  # type: ignore
except ImportError:
    requests_unixsocket = None  # type: ignore

# ---------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------


def log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def parse_bool(value: Optional[str], default: bool = False) -> bool:
    if value is None:
        return default
    value = value.strip().lower()
    if value in ("1", "true", "yes", "y", "on"):
        return True
    if value in ("0", "false", "no", "n", "off"):
        return False
    return default


def parse_aliases(raw: Optional[str], fallback: str) -> List[str]:
    """
    Parse the alias label into a list of aliases.

    Examples:
      - None or ""  -> [fallback]
      - "my-service" -> ["my-service"]
      - "alias1,alias2" -> ["alias1", "alias2"]
      - "alias1; alias2, alias3" -> ["alias1", "alias2", "alias3"]
    """
    if raw is None:
        return [fallback]

    text = str(raw).strip()
    if not text:
        return [fallback]

    # Allow both ',' and ';' as separators
    text = text.replace(";", ",")
    parts = [p.strip() for p in text.split(",")]
    aliases = [p for p in parts if p]

    return aliases or [fallback]


# container_id -> set of network names we manage for this container
managed_networks: Dict[str, Set[str]] = {}


# ---------------------------------------------------------
# Docker HTTP client
# ---------------------------------------------------------


class DockerAPI:
    def __init__(self, base_url: str, timeout: int = 5) -> None:
        self.timeout = timeout
        self.base_url, self.session = self._init_session(base_url)
        self.api_prefix = self._detect_api_prefix()

    def _init_session(self, base_url: str) -> tuple[str, requests.Session]:
        """
        Initialise la session HTTP en fonction du schéma :
        - unix://path → http+unix://%2Fpath avec requests_unixsocket
        - http/https/... → requests.Session classique
        """
        base_url = base_url.rstrip("/")

        if base_url.startswith("unix://"):
            if requests_unixsocket is None:
                raise RuntimeError(
                    "DOCKER_HOST uses unix:// but 'requests-unixsocket' is not installed "
                    "in the container image."
                )
            sock_path = base_url[len("unix://") :]
            encoded = quote(sock_path, safe="")
            session = requests_unixsocket.Session()  # type: ignore[arg-type]
            # requests-unixsocket utilise le schéma http+unix://
            return f"http+unix://{encoded}", session
        else:
            session = requests.Session()
            return base_url, session

    def _detect_api_prefix(self) -> str:
        url = f"{self.base_url}/version"
        try:
            resp = self.session.get(url, timeout=self.timeout)
            resp.raise_for_status()
            data = resp.json()
            api_version = data.get("ApiVersion") or data.get("APIVersion") or "1.52"
            log(f"Detected Docker API version {api_version}")
            return f"/v{api_version}"
        except Exception as e:  # noqa: BLE001
            # Fallback to Docker 29 API version which is 1.52 today
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
        params: Optional[dict] = None,
        stream: bool = False,
        timeout: Optional[int] = None,
    ) -> Response:
        url = self._url(path)
        return self.session.get(
            url,
            params=params,
            timeout=timeout or self.timeout,
            stream=stream,
        )

    def _post(self, path: str, payload: dict, timeout: Optional[int] = None) -> Response:
        url = self._url(path)
        return self.session.post(
            url,
            json=payload,
            timeout=timeout or self.timeout,
        )

    # High-level helpers

    def list_containers(self, all_containers: bool = True) -> List[dict]:
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
        aliases: Optional[List[str]] = None,
    ) -> None:
        """
        Connect a container to a network, optionally with one or more aliases.
        """
        payload: dict = {"Container": container_id}
        if aliases:
            payload["EndpointConfig"] = {"Aliases": aliases}
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

    def events_stream(self, filters: Optional[dict] = None):
        params: dict[str, Any] = {}
        if filters:
            params["filters"] = json.dumps(filters)
        # Keep the stream open; use a larger read timeout so we can reconnect if idle.
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

    # Network helpers

    def list_networks(self) -> List[dict]:
        resp = self._get("/networks")
        resp.raise_for_status()
        return resp.json()

    def inspect_network(self, network: str) -> dict:
        resp = self._get(f"/networks/{network}")
        resp.raise_for_status()
        return resp.json()

    def create_network(
        self,
        name: str,
        driver: str = "bridge",
        internal: bool = False,
        labels: Optional[dict] = None,
    ) -> dict:
        payload: dict = {
            "Name": name,
            "Driver": driver,
            "Internal": internal,
        }
        if labels:
            payload["Labels"] = labels
        resp = self._post("/networks/create", payload)
        resp.raise_for_status()
        return resp.json()

    def remove_network(self, network: str) -> None:
        url = self._url(f"/networks/{network}")
        resp = self.session.delete(url, timeout=self.timeout)
        resp.raise_for_status()


# ---------------------------------------------------------
# Config loading
# ---------------------------------------------------------


def load_config() -> dict:
    label_prefix = os.getenv("NETWORK_WATCHER_PREFIX", "network-watcher").strip()
    alias_label = os.getenv(
        "NETWORK_WATCHER_ALIAS_LABEL",
        f"{label_prefix}.alias",
    ).strip()
    detach_override_label = os.getenv(
        "NETWORK_WATCHER_DETACH_OVERRIDE_LABEL",
        f"{label_prefix}.detachdefault",
    ).strip()

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

    default_driver = os.getenv("NETWORK_WATCHER_DEFAULT_DRIVER", "bridge").strip() or "bridge"

    prune_unused = parse_bool(
        os.getenv("NETWORK_WATCHER_PRUNE_UNUSED_NETWORKS", "true"),
        True,
    )
    prune_orphan = parse_bool(
        os.getenv("NETWORK_WATCHER_PRUNE_ORPHAN_NETWORKS", "false"),
        False,
    )

    detach_other_global = parse_bool(
        os.getenv("NETWORK_WATCHER_DETACH_OTHER", "false"),
        False,
    )

    self_id_prefix = os.getenv("HOSTNAME", "").strip()

    log("Loaded network-watcher configuration:")
    log(f" Label prefix: {label_prefix}")
    log(f" Alias label: {alias_label}")
    log(f" Detach override label: {detach_override_label}")
    log(f" Initial attach: {initial_attach} (running only: {initial_running_only})")
    log(f" Auto-disconnect: {auto_disconnect}")
    log(f" Periodic rescan seconds: {rescan_seconds} (0 = disabled)")
    log(f" Debug: {debug}")
    log(f" Default network driver: {default_driver}")
    log(f" Prune unused networks: {prune_unused}")
    log(f" Prune orphan networks: {prune_orphan}")
    log(f" Detach other networks (global): {detach_other_global}")
    if self_id_prefix:
        log(f" Self container id prefix: {self_id_prefix}")
    else:
        log(" Self container id prefix:  (probably running outside Docker)")

    return {
        "label_prefix": label_prefix,
        "alias_label": alias_label,
        "detach_override_label": detach_override_label,
        "initial_attach": initial_attach,
        "initial_running_only": initial_running_only,
        "auto_disconnect": auto_disconnect,
        "rescan_seconds": rescan_seconds,
        "debug": debug,
        "default_driver": default_driver,
        "prune_unused_networks": prune_unused,
        "prune_orphan_networks": prune_orphan,
        "detach_other_global": detach_other_global,
        "self_id_prefix": self_id_prefix,
    }


# ---------------------------------------------------------
# Core helpers
# ---------------------------------------------------------


def get_base_url_from_env() -> str:
    """
    Choix de la cible Docker, dans l’ordre :
      1. DOCKER_HOST si présent.
      2. Sinon, si /var/run/docker.sock existe → unix:///var/run/docker.sock
      3. Sinon → http://socket-proxy:2375 (comportement historique).
    """
    host = os.getenv("DOCKER_HOST", "").strip()

    if host:
        # DOCKER_HOST explicite
        if host.startswith("tcp://"):
            host = "http://" + host[len("tcp://") :]
        # unix://... est géré plus tard dans DockerAPI._init_session
        if host.startswith("http://") or host.startswith("https://") or host.startswith("unix://"):
            return host.rstrip("/")
        # host:port ou hostname simple
        return f"http://{host.rstrip('/')}"
    else:
        # Pas de DOCKER_HOST → si socket monté, on l'utilise
        if os.path.exists("/var/run/docker.sock"):
            return "unix:///var/run/docker.sock"

        # Fallback historique: socket-proxy
        return "http://socket-proxy:2375"


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
    return ""


def get_network_mode(attrs: dict) -> Optional[str]:
    host_cfg = attrs.get("HostConfig") or {}
    mode = host_cfg.get("NetworkMode")
    if isinstance(mode, str):
        return mode
    return None


def container_has_watcher_labels(labels: dict, label_prefix: str) -> bool:
    prefix = f"{label_prefix}."
    for key in labels.keys():
        if isinstance(key, str) and key.startswith(prefix):
            return True
    return False


def extract_desired_networks(
    labels: dict,
    label_prefix: str,
) -> Tuple[Set[str], Set[str]]:
    """
    Return (desired_networks, referenced_networks) for a container.

    desired_networks -> networks where label value is truthy
    referenced_networks -> networks that have a label with this prefix
    regardless of the value, so we can later detach when label is removed.

    We treat keys of the form:
      <prefix>.<network>= true/false

    and ignore:
      <prefix>.alias
      <prefix>.detachdefault
      <prefix>.disablenetwork
      <prefix>.<network>.internal
    """
    desired: Set[str] = set()
    referenced: Set[str] = set()

    prefix = f"{label_prefix}."
    for key, value in labels.items():
        if not isinstance(key, str):
            continue
        if not key.startswith(prefix):
            continue

        suffix = key[len(prefix) :]

        # Ignore special keys
        if suffix in ("alias", "detachdefault", "disablenetwork"):
            continue
        if suffix.endswith(".internal"):
            continue

        net_name = suffix.strip()
        if not net_name:
            continue

        referenced.add(net_name)
        if parse_bool(str(value), False):
            desired.add(net_name)

    return desired, referenced


def extract_internal_preferences(
    labels: dict,
    label_prefix: str,
) -> Dict[str, bool]:
    """
    Look for labels of the form:
      <prefix>.<network>.internal: true/false

    Return mapping: network_name -> desired_internal (True/False).
    Only networks that have an explicit .internal label are returned.
    """
    result: Dict[str, bool] = {}

    prefix = f"{label_prefix}."
    for key, value in labels.items():
        if not isinstance(key, str):
            continue
        if not key.startswith(prefix):
            continue

        suffix = key[len(prefix) :]
        if not suffix.endswith(".internal"):
            continue

        base_name = suffix[: -len(".internal")].strip()
        if not base_name:
            continue

        desired_internal = parse_bool(str(value), False)
        result[base_name] = desired_internal

    return result


SYSTEM_NETWORKS = {"bridge", "host", "none", "ingress", "docker_gwbridge"}


def compute_referenced_networks(
    api: DockerAPI,
    containers: Optional[List[dict]] = None,
) -> Set[str]:
    """
    Return a set of network names that are referenced by any container,
    either via HostConfig.NetworkMode or NetworkSettings.Networks.
    """
    referenced: Set[str] = set()

    if containers is None:
        try:
            containers = api.list_containers(all_containers=True)
        except RequestException as e:
            log(f"[refs] Error listing containers to compute referenced networks: {e}")
            return referenced

    for c in containers:
        cid = c.get("Id") or c.get("ID")
        if not isinstance(cid, str):
            continue

        try:
            attrs = api.inspect_container(cid)
        except RequestException:
            continue

        mode = get_network_mode(attrs)
        if isinstance(mode, str) and mode:
            referenced.add(mode)

        nets = attrs.get("NetworkSettings", {}).get("Networks", {}) or {}
        for net_name in nets.keys():
            if isinstance(net_name, str) and net_name:
                referenced.add(net_name)

    return referenced


def ensure_network_exists_and_compatible(
    api: DockerAPI,
    net_name: str,
    desired_internal: Optional[bool],
    cfg: dict,
    reason: str,
) -> bool:
    """
    Ensure the network exists with the desired internal flag (if provided).

    If the network does not exist, create it using the default driver and
    internal flag (desired_internal or False).

    If it exists and desired_internal is not None and differs from current,
    recreate the network with the new internal setting, reattaching any
    previously attached containers.

    Return True on success (or best-effort), False on unrecoverable error.
    """
    debug: bool = cfg["debug"]
    default_driver: str = cfg["default_driver"]

    try:
        net = api.inspect_network(net_name)
        exists = True
    except RequestException as e:
        # 404 -> does not exist
        if (
            hasattr(e, "response")
            and getattr(e, "response", None) is not None
            and e.response is not None
            and e.response.status_code == 404
        ):
            exists = False
            net = None
        else:
            log(f"[{reason}] Error inspecting network '{net_name}': {e}")
            return False

    if not exists:
        internal = bool(desired_internal) if desired_internal is not None else False
        try:
            api.create_network(net_name, driver=default_driver, internal=internal)
            log(f"[{reason}] Created network '{net_name}' (internal={internal})")
        except RequestException as e:
            log(f"[{reason}] Failed to create network '{net_name}': {e}")
            return False
        return True

    # Network exists
    if desired_internal is None:
        # No requirement about internal flag
        return True

    current_internal = bool(net.get("Internal"))
    if current_internal == desired_internal:
        return True

    # Need to flip internal flag: detach all containers, remove, recreate, reattach
    containers_map = net.get("Containers") or {}
    attached_cids = list(containers_map.keys())

    if debug:
        log(
            f"[{reason}:internal] Recreating network '{net_name}' "
            f"to internal={desired_internal}, attached={attached_cids}"
        )

    # Best-effort detach
    for cid in attached_cids:
        try:
            api.disconnect_network(net_name, cid, force=True)
        except RequestException as e:
            log(
                f"[{reason}:internal] Failed to disconnect container {cid} "
                f"from '{net_name}' before recreation: {e}"
            )

    # Remove network
    try:
        api.remove_network(net_name)
    except RequestException as e:
        log(
            f"[{reason}:internal] Failed to remove network '{net_name}' "
            f"for recreation: {e}"
        )
        return False

    driver = net.get("Driver") or default_driver
    try:
        api.create_network(net_name, driver=driver, internal=desired_internal)
        log(
            f"[{reason}:internal-{'promote' if desired_internal else 'demote'}] "
            f"Recreated network '{net_name}' as "
            f"{'internal' if desired_internal else 'non-internal'} (driver={driver})."
        )
    except RequestException as e:
        log(f"[{reason}:internal] Failed to recreate network '{net_name}': {e}")
        return False

    # Reattach containers (best-effort, without alias knowledge here)
    for cid in attached_cids:
        try:
            api.connect_network(net_name, cid, aliases=None)
        except RequestException as e:
            log(
                f"[{reason}:internal] Failed to reattach container {cid} "
                f"to '{net_name}' after recreation: {e}"
            )

    return True


def maybe_prune_network(
    api: DockerAPI,
    net_name: str,
    cfg: dict,
    reason: str,
    containers_cache: Optional[List[dict]] = None,
) -> None:
    """
    Prune a single network if it is safe to do so:
      - not in SYSTEM_NETWORKS
      - not referenced by any container (NetworkMode or Networks)
      - has no attached containers (Containers map empty)

    This is used for targeted pruning of networks we previously managed for
    a given container (prune_unused_networks).
    """
    if not cfg["prune_unused_networks"]:
        return

    debug: bool = cfg["debug"]

    if net_name in SYSTEM_NETWORKS:
        return

    referenced = compute_referenced_networks(api, containers_cache)
    if net_name in referenced:
        return

    try:
        net = api.inspect_network(net_name)
    except RequestException as e:
        if debug:
            log(f"[{reason}] Failed to inspect network '{net_name}' for prune: {e}")
        return

    containers_map = net.get("Containers") or {}
    if containers_map:
        # Still has endpoints attached; do not remove.
        return

    try:
        api.remove_network(net_name)
        log(f"[{reason}] Removed unused network '{net_name}'")
    except RequestException as e:
        if debug:
            log(f"[{reason}] Failed to remove unused network '{net_name}': {e}")


def prune_orphan_networks(
    api: DockerAPI,
    cfg: dict,
    reason: str = "rescan:orphans",
    containers_cache: Optional[List[dict]] = None,
) -> None:
    """
    Global prune of orphan networks, controlled by NETWORK_WATCHER_PRUNE_ORPHAN_NETWORKS.

    A network is considered orphan if:
      - it is not a system network (bridge, host, none, ingress, docker_gwbridge)
      - it has no attached containers (Containers map empty)
      - it is not referenced by any container's NetworkMode or Networks.
    """
    if not cfg["prune_orphan_networks"]:
        return

    debug: bool = cfg["debug"]

    try:
        networks = api.list_networks()
    except RequestException as e:
        log(f"[{reason}] Error listing networks: {e}")
        return

    referenced = compute_referenced_networks(api, containers_cache)

    for net in networks:
        name = net.get("Name")
        if not isinstance(name, str) or not name:
            continue

        if name in SYSTEM_NETWORKS:
            continue

        # If any container references this network, do not prune it
        if name in referenced:
            continue

        containers_map = net.get("Containers") or {}
        if containers_map:
            continue

        try:
            api.remove_network(name)
            log(f"[{reason}] Removed orphan network '{name}'")
        except RequestException as e:
            if debug:
                log(f"[{reason}] Failed to remove orphan network '{name}': {e}")


# ---------------------------------------------------------
# Container reconciliation
# ---------------------------------------------------------


def reconcile_container(
    api: DockerAPI,
    container_id: str,
    cfg: dict,
    reason: str = "event",
) -> None:
    label_prefix: str = cfg["label_prefix"]
    alias_label: str = cfg["alias_label"]
    detach_override_label: str = cfg["detach_override_label"]
    auto_disconnect: bool = cfg["auto_disconnect"]
    debug: bool = cfg["debug"]
    detach_other_global: bool = cfg["detach_other_global"]
    self_id_prefix: str = cfg["self_id_prefix"]

    is_self = bool(self_id_prefix) and container_id.startswith(self_id_prefix)

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

    # If the container has no network-watcher-related labels at all,
    # do not touch its networks.
    if not container_has_watcher_labels(labels, label_prefix):
        if container_id in managed_networks:
            managed_networks.pop(container_id, None)
        if debug:
            log(f"[{reason}] Skipping '{name}': no {label_prefix}.* labels.")
        return

    # --------------------------------------------------
    # Global "disable all networks" label
    #   label: <prefix>.disablenetwork = true
    #   -> on détache tous les réseaux du conteneur
    # --------------------------------------------------
    disable_label_key = f"{label_prefix}.disablenetwork"
    disable_network = parse_bool(
        str(labels.get(disable_label_key)) if disable_label_key in labels else None,
        False,
    )

    if disable_network:
        attached_all = set(networks.keys())
        if attached_all:
            for net_name in sorted(attached_all):
                try:
                    api.disconnect_network(net_name, container_id, force=False)
                    log(
                        f"[{reason}] disablenetwork: disconnected '{name}' "
                        f"from network '{net_name}'"
                    )
                except RequestException as e:
                    log(
                        f"[{reason}] disablenetwork: failed to disconnect "
                        f"'{name}' from '{net_name}': {e}"
                    )

        # Ce conteneur est explicitement sans réseau géré.
        if container_id in managed_networks:
            managed_networks.pop(container_id, None)

        # On ne fait pas le reste de la logique (pas d'attach, pas de detach_other)
        return

    # --- logique normale si disablenetwork n'est pas actif ---

    desired_networks, referenced_networks = extract_desired_networks(
        labels,
        label_prefix,
    )
    internal_prefs = extract_internal_preferences(labels, label_prefix)

    prev_managed = managed_networks.get(container_id, set())
    # Union so we remember networks that were referenced in the past for
    # label-removal handling.
    managed = set(prev_managed) | set(referenced_networks)

    # Networks currently attached that we consider "managed" for this container
    attached_names = set(networks.keys())
    managed_attached = attached_names & managed

    to_connect = desired_networks - managed_attached
    to_disconnect = managed_attached - desired_networks

    # multi-alias support
    raw_alias = labels.get(alias_label)
    aliases = parse_aliases(raw_alias, fallback=name)

    if debug:
        log(
            f"[{reason}] {name}: desired={sorted(desired_networks)}, "
            f"managed_attached={sorted(managed_attached)}, "
            f"to_connect={sorted(to_connect)}, "
            f"to_disconnect={sorted(to_disconnect)}, "
            f"aliases={aliases}"
        )

    # First ensure networks exist / have proper internal flag for any network we might touch
    for net_name in sorted(desired_networks | referenced_networks):
        desired_internal = internal_prefs.get(net_name)
        ensure_network_exists_and_compatible(
            api,
            net_name,
            desired_internal,
            cfg,
            reason=reason,
        )

    # Attach networks whose label value is truthy
    for net_name in sorted(to_connect):
        try:
            if debug:
                log(
                    f"[{reason}] Connecting '{name}' to '{net_name}' "
                    f"with aliases {aliases}"
                )
            api.connect_network(net_name, container_id, aliases=aliases)
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

    # Optionally detach all *other* networks that are not referenced in labels
    # for this container, controlled by NETWORK_WATCHER_DETACH_OTHER and
    # per-container override label (detach_override_label).
    detach_other = detach_other_global and not is_self
    if detach_other and detach_override_label in labels:
        # If the override is explicitly false, we disable detach-other.
        if not parse_bool(str(labels.get(detach_override_label)), True):
            detach_other = False

    if detach_other:
        # Recompute attached networks (approximate: including newly connected, minus disconnected)
        effective_attached = (attached_names | to_connect) - to_disconnect

        # Keep all networks that are referenced by labels; detach the rest.
        to_detach_other = effective_attached - referenced_networks

        for net_name in sorted(to_detach_other):
            if net_name in SYSTEM_NETWORKS:
                continue
            try:
                api.disconnect_network(net_name, container_id, force=False)
                log(
                    f"[{reason}] detach_other: disconnected '{name}' "
                    f"from network '{net_name}'"
                )
            except RequestException as e:
                if debug:
                    log(
                        f"[{reason}] detach_other: failed to disconnect "
                        f"'{name}' from '{net_name}': {e}"
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

                if status in {"destroy", "die"}:
                    # Best-effort cleanup of our local cache
                    prev_managed = managed_networks.pop(cid, set())

                    # On container destroy, we may also prune networks we were managing
                    if status == "destroy" and cfg["prune_unused_networks"]:
                        # We pass containers_cache=None so compute_referenced_networks
                        # will fetch a fresh view.
                        for net_name in sorted(prev_managed):
                            maybe_prune_network(
                                api,
                                net_name,
                                cfg,
                                reason=f"event:{status}:prune",
                                containers_cache=None,
                            )

                if debug:
                    name = event.get("Actor", {}).get("Attributes", {}).get("name")
                    log(f"[event] Processing {status} for {name or cid}")

                reconcile_container(api, cid, cfg, reason=f"event:{status}")
        except ReadTimeout:
            # Normal when idle; just re-open the stream.
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

        # Also prune orphan networks if enabled, using same container cache
        prune_orphan_networks(api, cfg, reason="rescan:orphans", containers_cache=containers)


# ---------------------------------------------------------
# Healthcheck mode
# ---------------------------------------------------------


def healthcheck_main() -> int:
    """
    Mode healthcheck:
      - essaie de se connecter au Docker Engine
      - fait un appel léger list_containers
      - retourne 0 si OK, 1 si KO
    """
    try:
        base_url = get_base_url_from_env()
        api = DockerAPI(base_url=base_url, timeout=3)
        # Appel minimal : ne récupère que les conteneurs actifs
        api.list_containers(all_containers=False)
    except Exception as e:  # noqa: BLE001
        log(f"[healthcheck] FAIL: {e}")
        return 1

    # Pas de log en cas de succès pour éviter de spammer les logs toutes les X secondes
    return 0


# ---------------------------------------------------------
# Main entry
# ---------------------------------------------------------


def main() -> None:
    base_url = get_base_url_from_env()
    log(f"Connecting to Docker Engine via: {base_url}")

    try:
        api = DockerAPI(base_url=base_url)
    except Exception as e:  # noqa: BLE001
        log(f"Failed to create Docker HTTP client: {e}")
        sys.exit(1)

    cfg = load_config()

    # Optionally log watcher version (can be injected at build time)
    watcher_version = os.getenv("WATCHER_VERSION", "").strip()
    if watcher_version:
        log(f"docker-network-autoattach watcher version: {watcher_version}")

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
    # Mode healthcheck : `python watcher.py --healthcheck`
    if "--healthcheck" in sys.argv[1:]:
        sys.exit(healthcheck_main())

    main()
