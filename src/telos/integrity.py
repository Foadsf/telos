"""Out-of-Workspace Signing Key and HMAC Seals for every record that opens a gate.

Trust model: an agent that can only reach Telos through MCP, or that can edit files inside the
workspace, cannot forge state, because the key never lives in the workspace. An agent that can run
arbitrary code as the same OS user can still read the key; closing that gap needs the verifier to
run as a separate OS principal (service account, container), which is outside this module.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import sys
from typing import Any

from telos.toml_codec import dumps_canonical_toml, loads_toml


class IntegrityError(PermissionError):
    """Raised when gate-opening state lacks a valid seal (tampered, forged, or key missing)."""


def key_dir() -> Path:
    """Per-user key directory outside every workspace (`TELOS_KEY_DIR` overrides, e.g. for tests)."""
    override = os.environ.get("TELOS_KEY_DIR")
    if override:
        return Path(override).resolve()
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return (base / "telos" / "keys").resolve()


def canonical_json(data: Any) -> str:
    """Canonical text of a TOML-persisted record: normalised through the same TOML round trip as storage."""
    if isinstance(data, dict):
        data = loads_toml(dumps_canonical_toml(data))
    return json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)


class SealKeyring:
    """HMAC key bound to one workspace path, stored outside that workspace."""

    def __init__(self, workspace_root: Path) -> None:
        self.root = workspace_root.resolve()
        ws_id = hashlib.sha256(os.path.normcase(str(self.root)).encode("utf-8")).hexdigest()[:24]
        self.key_path = key_dir() / f"{ws_id}.key"
        if self.root == self.key_path.parent or self.root in self.key_path.parents:
            raise IntegrityError(
                f"Refusing a signing key inside the workspace ({self.key_path}); "
                f"point TELOS_KEY_DIR outside {self.root}."
            )

    def exists(self) -> bool:
        return self.key_path.exists()

    def create(self) -> None:
        if self.key_path.exists():
            return
        self.key_path.parent.mkdir(parents=True, exist_ok=True)
        self.key_path.write_bytes(secrets.token_bytes(32))
        try:
            os.chmod(self.key_path, 0o600)
        except OSError:
            pass

    def _key(self) -> bytes:
        if not self.key_path.exists():
            raise IntegrityError(
                f"No Telos signing key for workspace {self.root} on this machine. "
                f"A human must run `telos reseal` (or `telos init` for a new workspace)."
            )
        return self.key_path.read_bytes()

    def sign(self, payload: str) -> str:
        return hmac.new(self._key(), payload.encode("utf-8"), hashlib.sha256).hexdigest()

    def verify(self, payload: str, signature: str) -> bool:
        if not signature:
            return False
        return hmac.compare_digest(self.sign(payload), signature)

    def seal(self, data: Any) -> str:
        return self.sign(canonical_json(data))

    def check(self, data: Any, signature: str) -> bool:
        return self.verify(canonical_json(data), signature)
