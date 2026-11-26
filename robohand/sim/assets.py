"""Third-party model files, fetched once at a pinned commit into DATA_ROOT/sim_assets/."""
from __future__ import annotations

import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

from robohand.paths import DATA_ROOT


def fetch(name: str, raw_base: str, files: list[str]) -> Path:
    """Download ``files`` (paths relative to ``raw_base``) unless cached; return the cache root."""
    root = DATA_ROOT / "sim_assets" / name
    for relative in files:
        target = root / relative
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(f"{raw_base}/{relative}", timeout=60) as response:
            data = response.read()
        partial = target.with_name(target.name + ".part")
        partial.write_bytes(data)
        partial.replace(target)
    return root


def absolute_mesh_paths(model: ET.Element, mesh_dir: Path) -> None:
    """Point every <mesh file> at an absolute path and drop meshdir: absolute paths survive
    MjSpec.attach() and to_xml(), a meshdir does not."""
    compiler = model.find("compiler")
    if compiler is not None:
        compiler.attrib.pop("meshdir", None)
    for mesh in model.iter("mesh"):
        if mesh.get("file"):
            mesh.set("file", str((mesh_dir / mesh.get("file")).resolve()))
