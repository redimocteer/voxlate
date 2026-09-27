from pathlib import Path
import os
import shutil

from .common import load_config, read_json, write_json
from .runtime import bundle_root, set_external_code_root


def default_data_dir():
    return Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local/share")) / "voxlate"


def player_volume(data_dir):
    try:
        value = read_json(Path(data_dir)/'player-settings.json')['volume']
        return value if type(value) is int and 0 <= value <= 100 else 80
    except (OSError, ValueError, KeyError, TypeError):
        return 80


def save_player_volume(data_dir, value):
    if type(value) is not int or not 0 <= value <= 100:
        raise ValueError('播放器音量须为 0～100')
    write_json(Path(data_dir)/'player-settings.json', {'volume': value})


def prepare_settings(data_dir):
    data_dir = Path(data_dir).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    # Keep installation tools outside one-file's temporary extraction directory.
    support = data_dir / "support"
    source = bundle_root()
    for folder in ("voxlate", "scripts"):
        (support / folder).mkdir(parents=True, exist_ok=True)
        for path in (source / folder).glob("*.py"):
            shutil.copy2(path, support / folder / path.name)
    for name in ("requirements.txt", "requirements-prepare.txt", "README.md", "README.en.md", "TODO.md", "LICENSE", "THIRD_PARTY_NOTICES.md"):
        shutil.copy2(source / name, support / name)
    (support / "docs").mkdir(parents=True, exist_ok=True)
    for path in (source / "docs").glob("*.md"):
        shutil.copy2(path, support / "docs" / path.name)
    for path in (source / "docs/images").glob("*.png"):
        (support / "docs/images").mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, support / "docs/images" / path.name)
    set_external_code_root(support)
    config_path = data_dir / "config.json"
    defaults = read_json(source / "config.json")
    resources = data_dir / "resources"
    for section in ("runtime", "asr", "translator", "separator", "tts"):
        for key in ("python", "model_path", "repo_path", "engine_path"):
            if key in defaults[section]:
                defaults[section][key] = str(resources / defaults[section][key])
    if not config_path.exists():
        write_json(config_path, defaults)
    return config_path, defaults
