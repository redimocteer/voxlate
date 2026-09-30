"""Render real application widgets with synthetic data; no models or user media."""
import math
import os
from pathlib import Path
import shutil
import struct
import sys
import uuid
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtGui import QFontDatabase, QIcon
from PySide6.QtWidgets import QApplication
from voxlate.common import write_json
from voxlate.diagnostics import ResourceStatus
from voxlate.gui import MainWindow, STYLE
from voxlate.resources import CATALOG
from voxlate.role_dialog import RoleDialog
from voxlate.segmentation_dialog import SegmentationDialog, waveform_peaks


LINES = [
    (1.0, 3.1, 'Good morning. Ready to go?', '早上好，准备出发了吗？', 'a'),
    (3.8, 6.2, 'Yes. Let me get my camera.', '准备好了，我拿一下相机。', 'b'),
    (7.8, 11.0, 'The light is beautiful today.', '今天的光线真美。', 'a'),
    (11.7, 14.0, 'Shall we take the riverside path?', '我们走河边那条路吧？', 'b'),
    (15.0, 18.0, 'Good idea. It is quieter there.', '好主意，那边更安静。', 'a'),
    (18.8, 21.0, 'Wait for me!', '等等我！', 'c'),
    (22.0, 25.0, 'We will meet you by the bridge.', '我们在桥边等你。', 'b'),
    (26.0, 28.2, 'I will be there in a minute.', '我马上就到。', 'c'),
    (29.0, 32.0, 'Look at the clouds over the water.', '看，水面上方的云。', 'a'),
    (33.0, 36.0, 'This is a perfect place for a photo.', '这里很适合拍照。', 'b'),
    (37.0, 40.0, 'Let us remember this morning.', '让我们记住这个清晨。', 'c'),
]


def main():
    scratch = ROOT / 'temp' / ('readme-' + uuid.uuid4().hex)
    scratch.mkdir(parents=True)
    output = ROOT / 'docs' / 'images'
    output.mkdir(parents=True, exist_ok=True)
    app = QApplication([])
    fonts = Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Fonts'
    for name in ('msyh.ttc', 'msyhbd.ttc', 'segoeui.ttf'):
        if (fonts / name).exists():
            QFontDatabase.addApplicationFont(str(fonts / name))
    app.setStyle('Fusion')
    app.setStyleSheet(STYLE)
    app.setWindowIcon(QIcon(str(ROOT / 'assets' / 'voxlate.ico')))
    window = editor = roles = None

    def capture(widget, name):
        widget.show()
        app.processEvents()
        if not widget.grab().save(str(output / name)):
            raise RuntimeError(f'Could not save {name}')

    try:
        window = MainWindow(scratch / 'app-data', auto_check=False)
        project = dict(schema_version=1, name='voxlate', input=str(scratch / 'Morning walk.mp4'),
            duration=42, source_lang='en', target_lang='zh', recognition_model='asr_combined',
            voice_mode='roles', roles_initialized=True, prepared_audio_key='synthetic-demo',
            roles=[dict(id=k, name=n, reference_sentence_id=i) for k, n, i in
                   [('a', '旅人', 3), ('b', '摄影师', 2), ('c', '朋友', 8)]],
            segments=[dict(id=i, start=start, end=end, source_text=src, target_text=dst,
                           role_id=role, enabled=True)
                      for i, (start, end, src, dst, role) in enumerate(LINES, 1)])
        path = scratch / 'Morning walk.mp4.voxlate' / 'en-combined' / 'project.json'
        write_json(path, project)
        window.load_project(path)
        window.video.blockSignals(True)
        window.video.setText(r'D:\Videos\Morning walk.mp4')
        window.video.blockSignals(False)
        window.audio_track_box.setItemText(0, '音轨 1 · 英语 · 立体声')
        window.logs.setPlainText('示例项目 · 英文 → 中文\n已识别并翻译 11 句，可检查译文与角色后生成配音。')
        window.activity.setText('示例项目')
        window.tabs.setCurrentIndex(0)
        capture(window, 'dubbing.png')

        roles = RoleDialog(project, lambda ident: None, window)
        capture(roles, 'roles.png')
        roles.close()

        audio = path.parent / 'original.wav'
        rate = 16000
        with wave.open(str(audio), 'wb') as stream:
            stream.setparams((1, 2, rate, 0, 'NONE', 'not compressed'))
            for second in range(42):
                frames = []
                for sample in range(rate):
                    t = second + sample / rate
                    active = any(start < t < end for start, end, *_ in LINES)
                    envelope = (.16 + .15 * math.sin(t * 7) ** 2) if active else .003
                    frames.append(struct.pack('<h', int(24000 * envelope *
                        (math.sin(t * 1500) + .25 * math.sin(t * 2800)))))
                stream.writeframesraw(b''.join(frames))
        peaks = waveform_peaks(audio)
        editor = SegmentationDialog(project, 0, 3, audio, audio, [peaks, peaks], parent=window)
        editor.select_block(1)
        capture(editor, 'segmentation.png')
        editor.close()

        full_editor = SegmentationDialog(project, 0, len(project['segments'])-1,
            audio, audio, [peaks, peaks], parent=window, full=True)
        for start, end in ((1,3.1),(3.8,6.2),(7.8,9.2),(9.5,11),(11.7,14),(15,18),(22,25)):
            full_editor.plan.add_sentence(start,end)
        full_editor.canvas.span=18
        full_editor.render()
        full_editor.select_block(2)
        capture(full_editor, 'segmentation-full.png')
        full_editor.close()
        full_editor.deleteLater()

        # Illustrative resource status only; do not inspect or download real installations.
        window.resources_checked([ResourceStatus(k, info.title, True, '示例资源状态', '')
                                  for k, info in CATALOG.items()])
        window.storage.setText(r'D:\Voxlate\resources')
        window.tabs.setCurrentIndex(1)
        for i in range(window.resource_tree.topLevelItemCount()):
            item = window.resource_tree.topLevelItem(i)
            item.setExpanded(item.text(0) == '模型')
        window.install_logs.setPlainText('示例资源状态\n综合识别使用 Whisper large-v3 + turbo；也可选择独立识别模型。')
        window.activity.setText('示例资源状态')
        capture(window, 'resources.png')
    finally:
        for widget in (editor, roles, window):
            if widget is not None:
                widget.close()
                widget.deleteLater()
        app.processEvents()
        resolved = scratch.resolve()
        if resolved.parent != (ROOT / 'temp').resolve() or not resolved.name.startswith('readme-'):
            raise RuntimeError('Unexpected screenshot scratch path')
        shutil.rmtree(resolved)
    print(f'Saved five synthetic UI screenshots to {output}')


if __name__ == '__main__':
    main()
