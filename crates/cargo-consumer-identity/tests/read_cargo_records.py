"""Decode Cargo's actual TOML and JSON for the Rust verifier integration test."""

import json
import sys
import tomllib
from pathlib import Path

label, path = sys.argv[1:]
text = Path(path).read_text(encoding="utf-8")
packages = tomllib.loads(text)["package"] if label == "Cargo.lock" else json.loads(text)["packages"]
fields = (
    field
    for package in packages
    for field in ("package", package["name"], package.get("source") or "", package["version"])
)
sys.stdout.buffer.write("\0".join(fields).encode())
