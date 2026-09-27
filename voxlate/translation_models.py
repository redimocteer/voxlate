"""Pinned, independently installable translation choices."""
from pathlib import Path

MODELS = {
    "hy7_model": dict(kind="hy_mt2_7b", title="Hy-MT2 7B", relative="models/hy-mt2-7b-q4",
        size=4624648896, repo="tencent/Hy-MT2-7B-GGUF", revision="ab8472660ac61fac25f1af43fac2599d52a8a775",
        filename="Hy-MT2-7B-Q4_K_M.gguf", sha256="9f96256500f3fc1ab4d64336b58f52a949a95ad7516b0c229476eef782f9f77b"),
}
ENGINE_URL = "https://github.com/ggml-org/llama.cpp/releases/download/b11157/llama-b11157-bin-win-vulkan-x64.zip"
ENGINE_HASH = "b4dbe8a8bc4e2f20e1b8f178a61946b7ef74667350e31b6a1d31a6db16fdb573"


def selected_key(cfg):
    kind = cfg["translator"].get("model_type", "m2m100")
    return next((k for k, v in MODELS.items() if v["kind"] == kind), "hy7_model")


def select_model(cfg, key, root):
    item = MODELS[key]
    if cfg["translator"].get("model_type") != item["kind"]:
        cfg["translator"]["device"] = "cuda"
    cfg["translator"].update(model_type=item["kind"], model_path=str(Path(root) / item["relative"]),
                             engine_path=str(Path(root) / "tools/llama/llama-completion.exe"))
    return cfg
