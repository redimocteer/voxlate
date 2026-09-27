"""Render a local comparison from two existing video project profiles."""
import argparse
import html
from pathlib import Path
import re
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from voxlate.common import read_json
from voxlate.project_storage import validate_project_directory, project_root

p=argparse.ArgumentParser()
p.add_argument('first',type=Path)
p.add_argument('second',type=Path)
args=p.parse_args()
projects=[read_json(args.first),read_json(args.second)]
if Path(projects[0]['input']).resolve()!=Path(projects[1]['input']).resolve():
    raise ValueError('请选择同一视频的两个项目')
for path,project in zip((args.first,args.second),projects):
    validate_project_directory(project['input'],path.parent)
directory=project_root(projects[0]['input'])/'.temp/asr-comparison'
directory.mkdir(parents=True,exist_ok=True)
columns=[]
for project in projects:
    rows=[]
    for s in project['segments']:
        warning=' · 时间不确定' if s.get('timing_uncertain') else ''
        rows.append(f"<tr><td>{s['id']}</td><td>{s['start']:.2f}–{s['end']:.2f}{warning}</td>"
                    f"<td>{html.escape(s['source_text'])}<p>{html.escape(s.get('target_text',''))}</p></td></tr>")
    words=sum(len(re.findall(r"\b[\w’']+\b",s['source_text'])) for s in project['segments'])
    columns.append(f"<section><h2>{html.escape(project.get('recognition_model','识别结果'))}</h2>"
                   f"<p>{len(rows)} 句 · 英文分词计数 {words}（数量不代表准确率）</p><table>{''.join(rows)}</table></section>")
output=directory/'comparison.html'
output.write_text('<!doctype html><meta charset="utf-8"><title>识别结果对照</title><style>body{font:15px sans-serif;margin:24px;color:#20334e}main{display:grid;grid-template-columns:1fr 1fr;gap:24px}table{border-collapse:collapse;width:100%}td{border-bottom:1px solid #ddd;padding:8px;vertical-align:top}p{color:#61728a}</style><h1>同一视频的识别结果</h1><p>左右独立分句，请按时间对照。没有人工标准稿，不能仅凭句数判断哪款更准确。</p><main>'+''.join(columns)+'</main>',encoding='utf-8')
print(output)
