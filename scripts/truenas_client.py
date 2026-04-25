"""JSON-RPC 2.0 over WebSocket client for TrueNAS Scale 25.x.

TrueNAS dropped the REST `/api/v2.0/*` surface in favor of JSON-RPC over
WebSocket starting with the 25.x line. The endpoint is version-pinned, e.g.

    wss://truenas.lan/api/v25.10.2

Usage:
    from truenas_client import TruenasClient
    with TruenasClient(ws_url, api_key=os.environ["TRUENAS_API_KEY"]) as c:
        apps = c.app_query()
        cfg  = c.app_config("plex")

Auth happens automatically on `connect()` via `auth.login_with_api_key`.
"""

from __future__ import annotations

import json
import ssl
from typing import Any

try:
    import websocket  # provided by the `websocket-client` pip package
except ImportError as e:  # pragma: no cover
    raise SystemExit(
        "the `websocket-client` package is required; install it with "
        "`pip install websocket-client` or re-run scripts/bootstrap.sh"
    ) from e


class TruenasError(RuntimeError):
    pass


class TruenasClient:
    def __init__(
        self,
        ws_url: str,
        api_key: str,
        *,
        username: str = "admin",
        timeout: int = 60,
        verify_tls: bool = True,
    ):
        self.ws_url = ws_url
        self.api_key = api_key
        self.username = username
        self.timeout = timeout
        self.verify_tls = verify_tls
        self._ws: websocket.WebSocket | None = None
        self._next_id = 0

    def __enter__(self) -> "TruenasClient":
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def connect(self) -> None:
        sslopt = None if self.verify_tls else {"cert_reqs": ssl.CERT_NONE}
        self._ws = websocket.create_connection(self.ws_url, timeout=self.timeout, sslopt=sslopt)
        # 25.x ties API keys to a user; auth.login_ex with API_KEY_PLAIN is the
        # canonical path. The legacy auth.login_with_api_key call returns False.
        result = self._call(
            "auth.login_ex",
            [{
                "mechanism": "API_KEY_PLAIN",
                "username": self.username,
                "api_key": self.api_key,
            }],
        )
        # auth.login_ex returns a dict like {"response_type": "SUCCESS", ...}.
        if not isinstance(result, dict) or result.get("response_type") != "SUCCESS":
            raise TruenasError(
                f"auth.login_ex failed: {result!r} "
                f"(check the username '{self.username}' and the API key)"
            )

    def close(self) -> None:
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:
                pass
            self._ws = None

    # ------------------------------------------------------------------ rpc

    def _call(self, method: str, params: list | dict | None = None) -> Any:
        if self._ws is None:
            raise TruenasError("not connected")
        self._next_id += 1
        request_id = self._next_id
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            payload["params"] = params
        self._ws.send(json.dumps(payload))

        # TrueNAS sends server-side notifications on the same socket; skip
        # anything without our request id.
        while True:
            raw = self._ws.recv()
            if not raw:
                continue
            msg = json.loads(raw)
            if msg.get("id") != request_id:
                continue
            if msg.get("error"):
                raise TruenasError(f"{method}: {msg['error']}")
            return msg.get("result")

    # ------------------------------------------------------------------ apps

    def app_query(self, *, filters: list | None = None, options: dict | None = None) -> list[dict]:
        return self._call("app.query", [filters or [], options or {}]) or []

    def app_config(self, name: str) -> dict:
        """Return the full config object for an app, including the compose body."""
        return self._call("app.config", [name]) or {}

    def app_create(self, *, name: str, compose_yaml: str) -> Any:
        return self._call(
            "app.create",
            [{
                "app_name": name,
                "custom_app": True,
                "custom_compose_config_string": compose_yaml,
                "values": {},
            }],
        )

    def app_update(self, *, name: str, compose_yaml: str) -> Any:
        return self._call(
            "app.update",
            [name, {"custom_compose_config_string": compose_yaml}],
        )

    def app_delete(
        self,
        *,
        name: str,
        remove_images: bool = False,
        remove_ix_volumes: bool = False,
    ) -> None:
        self._call(
            "app.delete",
            [name, {"remove_images": remove_images, "remove_ix_volumes": remove_ix_volumes}],
        )

    # ------------------------------------------------------------ filesystem

    def fs_stat(self, path: str) -> dict | None:
        """Return stat info for `path`, or None if it doesn't exist."""
        try:
            return self._call("filesystem.stat", [path])
        except TruenasError:
            return None

    def fs_mkdir(self, path: str, *, mode: str = "755") -> dict:
        """Create a directory at `path` (no recursive parent creation)."""
        return self._call(
            "filesystem.mkdir",
            [{"path": path, "options": {"mode": mode}}],
        )

    def fs_setperm(
        self,
        path: str,
        *,
        user: str | None = None,
        group: str | None = None,
        mode: str | None = None,
        recursive: bool = False,
        stripacl: bool = False,
    ) -> Any:
        body: dict = {"path": path}
        if user is not None:
            body["user"] = user
        if group is not None:
            body["group"] = group
        if mode is not None:
            body["mode"] = mode
        body["options"] = {"recursive": recursive, "stripacl": stripacl}
        return self._call("filesystem.setperm", [body])

    # ------------------------------------------------------------ datasets

    def dataset_query(self, name: str) -> dict | None:
        """Return the dataset record for the full path (e.g. 'tank/apps/x'), or None."""
        res = self._call("pool.dataset.query", [[["id", "=", name]], {"limit": 1}])
        if isinstance(res, list) and res:
            return res[0]
        return None

    def dataset_create(
        self,
        name: str,
        *,
        type: str = "FILESYSTEM",
        properties: dict | None = None,
    ) -> dict:
        payload: dict = {"name": name, "type": type}
        if properties:
            payload.update(properties)
        return self._call("pool.dataset.create", [payload])
