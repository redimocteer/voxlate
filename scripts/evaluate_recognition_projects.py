"""Read existing profiles and save measurements beside their original video."""
import argparse
import csv
import difflib
import hashlib
import json
from pathlib import Path
import re
import statistics
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from voxlate.common import read_json, write_json
from voxlate.project_storage import validate_project_directory

parser=argparse.ArgumentParser()
parser.add_argument('project_root',type=Path)
args=parser.parse_args()
root=args.project_root.resolve()
profiles=[('Whisper v3','en-whisper-large-v3'),('Whisper turbo','en-whisper-large-v3-turbo'),
          ('综合 v3+turbo','en-combined'),('Qwen3-ASR','en-qwen3-asr-1.7b')]
out=root/'.temp/model-evaluation'
out.mkdir(parents=True,exist_ok=True)
metrics=[]; sentence_rows=[]; projects={}
def elapsed(log,label):
    found=re.findall(r'(?m)^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} '+re.escape(label)+r' · (?:总)?耗时 (\d+):(\d+):(\d+)',log)
    return [int(h)*3600+int(m)*60+int(s) for h,m,s in found]
def characters(text):
    return sum(c.isalnum() for c in text)
for name,folder in profiles:
    directory=root/folder
    p=read_json(directory/'project.json'); projects[folder]=p
    validate_project_directory(p['input'],directory)
    segments=p['segments']; ds=[s['end']-s['start'] for s in segments]
    lens=[characters(s['target_text']) for s in segments]
    log=(directory/'run.log').read_text(encoding='utf-8-sig')
    job=read_json(directory/'translator_job.json'); translated=read_json(directory/'translator_result.json')
    translator_text_matches=len(translated)==len(segments) and all(a==b['target_text'] for a,b in zip(translated,segments))
    asrjob=read_json(directory/'asr_job.json')
    audio=Path(asrjob['audio'])
    with audio.open('rb') as f: audio_hash=hashlib.file_digest(f,'sha256').hexdigest()
    m=dict(model=name,profile=folder,video_seconds=p['duration'],input_hash=p.get('input_hash'),
           source_audio_sha256=audio_hash,translator=job['config'].get('model_type'),
           translator_settings=job['config'],asr_settings=asrjob['config'],
           translation_matches_generated_result=translator_text_matches,
           sentences=len(segments),enabled=sum(s.get('enabled',True) for s in segments),
           median_seconds=statistics.median(ds),max_seconds=max(ds),
           over5=sum(d>5 for d in ds),over8=sum(d>8 for d in ds),under_half=sum(d<.5 for d in ds),
           covered_seconds=sum(ds),source_words=sum(len(re.findall(r"\b[\w’']+\b",s['source_text'])) for s in segments),
           target_characters=sum(lens),max_target_characters=max(lens),
           dense_over8=[s['id'] for s,d,n in zip(segments,ds,lens) if d>=1 and n/d>8],
           timing_uncertain=[s['id'] for s in segments if s.get('timing_uncertain')],
           asr_seconds=elapsed(log,'识别完成'),translation_seconds=elapsed(log,'翻译完成'),
           recognition_translation_total_seconds=elapsed(log,'识别并翻译完成'),
           dubbing_total_seconds=elapsed(log,'生成配音完成'),
           covers_38=[s['id'] for s in segments if s['start']<=38<s['end']],
           covers_14=[s['id'] for s in segments if s['start']<=14<s['end']],
           longest_ids=sorted(range(len(ds)),key=lambda i:ds[i],reverse=True)[:4])
    m['longest_ids']=[segments[i]['id'] for i in m['longest_ids']]
    if (directory/'tts_metrics.json').exists():
        m['tts']=read_json(directory/'tts_metrics.json')
        ttslog=(directory/'tts.log').read_text(encoding='utf-8-sig')
        times={}; current=None
        for line in ttslog.splitlines():
            if match:=re.match(r'中文配音 (\d+)/(\d+)',line): current=int(match[1]);times[current]=[]
            if current is not None and (match:=re.search(r'Total inference time: ([\d.]+) seconds',line)):
                times[current].append(float(match[1]))
        m['tts_internal_seconds']={str(k):sum(v) for k,v in times.items()}
        m['tts_internal_chunks']={str(k):v for k,v in times.items() if len(v)>1}
    metrics.append(m)
    for s,d,n in zip(segments,ds,lens):
        sentence_rows.append(dict(model=name,id=s['id'],start=s['start'],end=s['end'],seconds=round(d,3),
             target_characters=n,characters_per_second=round(n/d,2),uncertain=s.get('timing_uncertain',False),
             source=s['source_text'],translation=s['target_text']))
combined=projects['en-combined']['segments']; v3=projects['en-whisper-large-v3']['segments']
key=lambda s:(s['source_text'],round(s['start'],3),round(s['end'],3))
extra=[s['id'] for s in combined if key(s) not in set(map(key,v3))]
review=read_json(root/'en-combined/recognition/review.json')
summary=dict(metrics=metrics,combined_unchanged_v3_rows=sum(key(s) in set(map(key,combined)) for s in v3),
             combined_extra_ids=extra,combined_review_windows=len(review['windows']),combined_changes=review['changes'])
write_json(out/'metrics.json',summary)
with (out/'sentences.csv').open('w',encoding='utf-8-sig',newline='') as f:
    writer=csv.DictWriter(f,fieldnames=sentence_rows[0].keys());writer.writeheader();writer.writerows(sentence_rows)
lines=['# 四种识别结果统计','',f"视频时长：{metrics[0]['video_seconds']:.2f} 秒。测量现有文件，不重新运行模型。",
       '所有方案均使用 Hy-MT2 7B 翻译。时间为日志中的本机实测，模型加载包含在识别阶段内；Qwen 同时包含时间对齐。',
       '没有人工标准稿，不能据此计算识别准确率或词错误率。句数、覆盖时长、字数均不是准确率。','',
       '| 方案 | 句数 | 中位秒数 | 最长秒数 | >5秒 | <0.5秒 | 最长译文字符 | 识别秒数 | 翻译秒数 |',
       '|---|---:|---:|---:|---:|---:|---:|---:|---:|']
for m in metrics:
    lines.append(f"| {m['model']} | {m['sentences']} | {m['median_seconds']:.2f} | {m['max_seconds']:.2f} | {m['over5']} | {m['under_half']} | {m['max_target_characters']} | {m['asr_seconds'][-1]} | {m['translation_seconds'][-1]} |")
lines+=['','字符数只计汉字、字母和数字，不计空格及标点。1 秒以上句子中超过 8 字符/秒，只作为文字密度指标，不等于实测配音加速倍率。',
        '',f"综合识别与 v3 原文及时间完全一致的句子：{summary['combined_unchanged_v3_rows']}/{len(v3)}；额外句子：{extra}。",
        f"综合识别复查 {len(review['windows'])} 个窗口，实际变动 {len(review['changes'])} 处。",'',
        '## 配音耗时与译文密度','','| 方案 | 译文字符合计 | >8字符/秒的句号（仅计≥1秒句子） | 配音合成秒数（不含加载） | 模型加载秒数 | 完整生成配音秒数 |',
        '|---|---:|---|---:|---:|---:|']
for m in metrics:
    t=m.get('tts')
    lines.append(f"| {m['model']} | {m['target_characters']} | {','.join(map(str,m['dense_over8'])) or '无'} | "
                 +(f"{t['generation_seconds']:.2f} | {t['loading_seconds']:.2f} | {m['dubbing_total_seconds'][-1]} |" if t else '未测 | 未测 | 未测 |'))
lines+=['','四份识别人声音轨是否完全相同：'+str(len({m['source_audio_sha256'] for m in metrics})==1),
        '当前译文是否均与翻译原始输出一致：'+str(all(m['translation_matches_generated_result'] for m in metrics)),
        '未完成配音的两种方案不推算合成时间。文字密度只用于筛选待检查句子；识别漏词、翻译增删及时间错位都可能改变指标。']
for m in metrics:
    if 'tts' in m and m['tts_internal_seconds']:
        ident,seconds=max(m['tts_internal_seconds'].items(),key=lambda item:item[1])
        share=seconds/m['tts']['generation_seconds']*100
        lines+=['',f"{m['model']} 最慢为第 {ident} 句，内部合成累计 {seconds:.2f} 秒，占整批合成时间约 {share:.1f}%。"]
lines+=['','## 逐句结果']
for name,_ in profiles:
    lines+=['',f'### {name}','','| 编号 | 时间 | 原文 | 译文 |','|---:|---|---|---|']
    for row in sentence_rows:
        if row['model']==name:
            lines.append(f"| {row['id']} | {row['start']:.2f}–{row['end']:.2f} | {row['source'].replace('|','/')} | {row['translation'].replace('|','/')} |")
(out/'report.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
print(json.dumps(summary,ensure_ascii=False,indent=2))
