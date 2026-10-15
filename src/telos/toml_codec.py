"""Deterministic, Git-merge-friendly canonical TOML serializer and parser."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from pathlib import Path
import re
import tomllib
from typing import Any

_BARE_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")

def _escape_toml_str(val: str) -> str:
    if "\n" in val and '"""' not in val:
        return f'"""\n{val}"""'
    escaped = (
        val.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\b", "\\b")
        .replace("\t", "\\t")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
    )
    return f'"{escaped}"'

def _format_key(key: str) -> str:
    if _BARE_KEY_RE.match(key):
        return key
    return _escape_toml_str(key)

def _format_scalar(val: Any) -> str:
    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, int):
        return str(val)
    if isinstance(val, float):
        return f"{val:.6g}" if val % 1 != 0 else f"{val:.1f}"
    if isinstance(val, datetime):
        return f'"{val.isoformat()}"'
    if isinstance(val, Enum):
        return _escape_toml_str(val.value)
    if isinstance(val, str):
        return _escape_toml_str(val)
    if isinstance(val, list):
        items = ", ".join(_format_scalar(item) for item in val)
        return f"[{items}]"
    raise TypeError(f"Unsupported scalar type for TOML serialization: {type(val)}")

def dumps_canonical_toml(data: dict[str, Any], prefix: str = "") -> str:
    """Serialize a dictionary into canonical, deterministically sorted TOML."""
    lines: list[str] = []
    scalar_keys: list[str] = []
    table_keys: list[str] = []
    array_of_tables_keys: list[str] = []

    for key in sorted(data.keys()):
        val = data[key]
        if val is None:
            continue
        if isinstance(val, dict):
            table_keys.append(key)
        elif isinstance(val, list) and val and all(isinstance(x, dict) for x in val):
            array_of_tables_keys.append(key)
        else:
            scalar_keys.append(key)

    for key in scalar_keys:
        lines.append(f"{_format_key(key)} = {_format_scalar(data[key])}")

    for key in table_keys:
        fmt_k = _format_key(key)
        sub_prefix = f"{prefix}.{fmt_k}" if prefix else fmt_k
        sub_dict = {k: v for k, v in data[key].items() if v is not None}
        if lines:
            lines.append("")
        lines.append(f"[{sub_prefix}]")
        sub_body = dumps_canonical_toml(sub_dict, prefix=sub_prefix).strip()
        if sub_body:
            lines.append(sub_body)

    for key in array_of_tables_keys:
        fmt_k = _format_key(key)
        sub_prefix = f"{prefix}.{fmt_k}" if prefix else fmt_k
        for item in data[key]:
            if lines:
                lines.append("")
            lines.append(f"[[{sub_prefix}]]")
            sub_body = dumps_canonical_toml(item, prefix=sub_prefix).strip()
            if sub_body:
                lines.append(sub_body)

    return "\n".join(lines) + "\n"

def loads_toml(text: str) -> dict[str, Any]:
    return tomllib.loads(text)

def write_canonical_toml(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".toml.tmp")
    tmp_path.write_text(dumps_canonical_toml(data), encoding="utf-8")
    tmp_path.replace(path)

def read_toml(path: Path) -> dict[str, Any]:
    return tomllib.loads(path.read_text(encoding="utf-8"))
