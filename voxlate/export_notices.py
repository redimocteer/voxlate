"""Offline notices saved with the project, never a record of local user paths."""
from pathlib import Path
import hashlib
import json

from .common import VoxlateError, write_json
from .runtime import bundle_root

NON_ENDORSEMENT = (
    'Any modifications made to the original model in this Derivative Work are not endorsed, '
    'warranted, or guaranteed by the original right-holder of the original model, and the '
    'original right-holder disclaims all liability related to this Derivative Work.'
)


def write_export_notices(project_directory, model_directory):
    target = Path(project_directory) / 'export-notices'
    target.mkdir(parents=True, exist_ok=True)
    bundled = bundle_root() / 'assets/legal'
    name = 'IndexTTS-2.5-LICENSE.txt'
    record = json.loads((bundled / 'SOURCES.json').read_text(encoding='utf-8'))[name]
    data = (bundled / name).read_bytes()
    if hashlib.sha256(data).hexdigest() != record['sha256']:
        raise VoxlateError('随附模型协议校验失败，请重新安装完整程序。')
    (target / name).write_bytes(data)
    write_json(target / 'SOURCES.json', {name: record})
    # A manually supplied model may carry a different agreement. Preserve it
    # separately rather than claiming our reference snapshot covers that model.
    actual = Path(model_directory) / 'LICENSE'
    actual_name = 'Installed-model-LICENSE.txt'
    if actual.is_file():
        (target / actual_name).write_bytes(actual.read_bytes())
    else:
        (target / actual_name).unlink(missing_ok=True)
    text = (
        '配音导出与分享说明\n\n'
        '本项目配音包含 AI 翻译／合成声音，不代表原说话者真实发言或认可。\n'
        'Voxlate 原创程序的 MIT 许可不覆盖您的视频、音乐、声音及模型输出。\n'
        '分享前请确认素材及声音授权、人工核对内容，并按适用规则主动声明、使用平台 AI 标识。\n'
        '配音音轨名称中的 AI 字样不是完整的法定显式／隐式标识实现；不能保证转码保留全部输入标识。\n\n'
        '音色合成使用 IndexTTS。随附协议是 Voxlate 支持的固定 IndexTTS 2.5 上游版本副本；'
        '自选或旧模型应核对其实际版本，不能据此推定许可一致。'
        '如模型目录存在 LICENSE，另存为 Installed-model-LICENSE.txt。\n'
        '上游协议涉及输出及衍生物、原始声明与协议副本保留、向下游传递条款以及规模门槛。'
        '发布或转交生成内容时，请一并提供适用的协议和声明，并按协议落实下游义务；'
        '仅传送视频或本说明不等于自动满足所有合同、版权及监管要求。\n\n'
        '上游要求的非背书声明：\n' + NON_ENDORSEMENT + '\n\n'
        '本目录供您与导出视频一并分享。不会自动上传。许可来源和校验值见 SOURCES.json。\n'
    )
    (target / '分享前请读.txt').write_text(text, encoding='utf-8-sig')
    return target
