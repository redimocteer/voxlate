from pathlib import Path

from .common import VoxlateError


def separate(audio, directory, config):
    # Local repository is mandatory: Demucs cannot fall back to its remote registry.
    from demucs.separate import main

    argv = ["--repo", config["model_path"], "-n", config["model"], "-d", config["device"],
            "--two-stems", "vocals", "--shifts", str(config["shifts"]),
            "--overlap", str(config["overlap"]), "-o", str(directory), str(audio)]
    main(argv)
    root = Path(directory) / config["model"] / Path(audio).stem
    vocals, background = root / "vocals.wav", root / "no_vocals.wav"
    if not vocals.is_file() or not background.is_file():
        raise VoxlateError("人声分离未生成 vocals.wav / no_vocals.wav")
    return str(vocals), str(background)
