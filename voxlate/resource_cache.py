"""Persist last-known resource status; startup never imports model runtimes."""
from dataclasses import asdict
from pathlib import Path

from .common import digest, read_json, write_json
from .diagnostics import ResourceStatus
from .resources import CATALOG, EXTRAS, ResourceInspection


def signature(cfg):
    parts = {key: cfg.get(key) for key in ("resource_root", "ffmpeg", "ffprobe", "runtime", "qwen", "asr", "translator", "separator", "tts")}
    return digest(1, parts)


def save_resource_cache(cfg, config_path, results):
    expected = set(CATALOG) | {"ffprobe"}
    if {r.key for r in results} != expected:
        return
    write_json(Path(config_path).with_name("resource-status.json"), dict(signature=signature(cfg),
        results=[asdict(r) for r in results], sizes=getattr(results, "sizes", {}),
        estimates=getattr(results, "estimates", {})))


def load_resource_cache(cfg, config_path):
    try:
        data = read_json(Path(config_path).with_name("resource-status.json"))
        if data["signature"] != signature(cfg):
            return None
        results = [ResourceStatus(**item) for item in data["results"]]
        if {r.key for r in results} != set(CATALOG) | {"ffprobe"}:
            return None
        keys = set(CATALOG) | set(EXTRAS)
        sizes = {k: v for k, v in data["sizes"].items() if k in keys and type(v) is int and v >= 0}
        return ResourceInspection(results, sizes, data["estimates"], "")
    except (OSError, ValueError, KeyError, TypeError):
        return None
