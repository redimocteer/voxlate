import argparse
import logging
from pathlib import Path
import shutil
import sys

from .common import VoxlateError, enable_offline, load_config, read_json
from .pipeline import VideoDubPipeline
from .project_storage import existing_project_directory, validate_project_directory


def doctor(cfg, config_path):
    from .diagnostics import check_resources
    results = check_resources(cfg, config_path)
    for item in results:
        print(f"[{'通过' if item.ready else ('需要配置' if item.required else '可选')}] {item.title}: {item.detail}")
    if any(not item.ready and item.required for item in results):
        print("打开 GUI 的「资源配置」查看逐项完成步骤，或按 README 准备资源。")
        return 1
    print("资源检查通过；模型实际加载和推理仍需用短视频验证。")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="voxlate", description="纯本地英文或日文→中文视频配音，保留音色与背景声")
    parser.add_argument("input", nargs="?", type=Path)
    parser.add_argument("--config", type=Path, default=Path(__file__).resolve().parents[1] / "config.json")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--source-lang", choices=["en", "ja"], help="视频源语言，默认使用配置")
    parser.add_argument('--audio-track', type=int, help='原声音轨编号，从 1 开始；默认第 1 条')
    parser.add_argument("--work-dir", type=Path)
    parser.add_argument("--speaker-ref", type=Path, help="5–15 秒干净的人物参考音频；省略则从分离人声选取")
    stages = parser.add_mutually_exclusive_group()
    stages.add_argument("--stop-after", choices=["recognize", "translate", "dub"], help="完成识别、翻译或配音后暂停")
    stages.add_argument('--translate-only', action='store_true', help='仅翻译已识别的对白')
    stages.add_argument('--auto-export', action='store_true', help='接续已有结果，一键导出')
    stages.add_argument('--export-only', action='store_true', help='只使用已生成的配音导出视频')
    parser.add_argument("--doctor", action="store_true", help="检查程序、环境和关键模型文件")
    parser.add_argument("--overwrite", action="store_true", help="允许替换已有输出视频")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    enable_offline()
    try:
        cfg = load_config(args.config)
        if args.source_lang:
            cfg["source_lang"] = args.source_lang
        if args.audio_track is not None:
            if args.audio_track < 1:
                raise VoxlateError('音轨编号须从 1 开始')
            cfg['audio_track'] = args.audio_track-1
        if args.doctor:
            return doctor(cfg, args.config)
        if args.input is None:
            parser.error("请指定输入视频，或使用 --doctor")
        from .audio_tracks import read_audio_tracks, track_suffix
        try:
            cfg['audio_track_count'] = len(read_audio_tracks(args.input.resolve(), cfg))
        except VoxlateError:
            # Translation-only can work without media tools using saved rows.
            cfg['audio_track_count'] = 0
        work = args.work_dir or existing_project_directory(args.input, cfg)
        if not cfg['audio_track_count'] and (work/'project.json').is_file():
            cfg['audio_track_count'] = read_json(work/'project.json').get('audio_track_count', 0)
        suffix = track_suffix(cfg)
        output = args.output or args.input.resolve().with_name(f"{args.input.stem}{suffix}.zh.mp4")
        validate_project_directory(args.input, work)
        if output.exists() and not args.overwrite and not args.stop_after and not args.translate_only:
            raise VoxlateError("输出已存在，请换一个 --output 或使用 --overwrite")
        result = VideoDubPipeline(cfg).process(args.input, output,
                 work, args.speaker_ref, 'translate' if args.translate_only else args.stop_after,
                 export_only=args.export_only, translate_only=args.translate_only, auto_export=args.auto_export)
        print(f"已完成：{result}")
        return 0
    except KeyboardInterrupt:
        logging.error("已停止，已完成的阶段和单句配音保留，下次可继续")
        return 130
    except (VoxlateError, OSError, ValueError, KeyError, TypeError) as exc:
        logging.error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
