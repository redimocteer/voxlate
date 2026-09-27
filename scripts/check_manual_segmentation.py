"""Exercise real local ASR/translation on an isolated copy inside the video project."""
import argparse
import copy
import json
import logging
import os
from pathlib import Path
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from voxlate.common import load_config, read_json, write_json, digest
from voxlate.project_storage import validate_project_directory
from voxlate.segmentation import apply_segmentation, recognize_segment
from voxlate.paired_segments import PairedSegmentPlan


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('project',type=Path)
    parser.add_argument('--config',required=True,type=Path)
    args=parser.parse_args()
    source=args.project.resolve()
    project=read_json(source)
    validate_project_directory(project['input'],source.parent)
    original=source.read_bytes()
    candidates=[(i,s) for i,s in enumerate(project['segments']) if 2 <= s['end']-s['start'] <= 6 and len(s.get('words',[])) >= 4]
    if not candidates:
        raise RuntimeError('Need a 2–6 second source row with word timings for this smoke check')
    index,row=max(candidates,key=lambda pair:pair[1]['end']-pair[1]['start'])
    cut=row['words'][len(row['words'])//2]['start']
    plan=PairedSegmentPlan(project,index,index)
    plan.cuts=[row['start'],cut,cut,row['end']]
    plan.validate()
    destination=source.parent/'.temp'/('manual-smoke-'+uuid.uuid4().hex[:10])
    destination.mkdir(parents=True)
    model=project['separator_model']
    for relative in [Path('original.wav'),Path('separated')/model/'original/vocals.wav',Path('separated')/model/'original/no_vocals.wav']:
        target=destination/relative
        target.parent.mkdir(parents=True,exist_ok=True)
        os.link(source.parent/relative,target)
    copy_path=destination/'project.json'
    write_json(copy_path,copy.deepcopy(project))
    cfg=load_config(args.config)
    cfg['source_lang']=project.get('source_lang','en')
    logging.basicConfig(level=logging.INFO,format='%(message)s',handlers=[logging.FileHandler(destination/'check.log',encoding='utf-8')])
    started=time.perf_counter()
    text=recognize_segment(copy_path,digest(project),cfg,*plan.pairs[0])
    preview_seconds=time.perf_counter()-started
    blocks=plan.blocks
    next(b for b in blocks if b['enabled']).update(text=text,manual_text=True)
    result=apply_segmentation(copy_path,digest(project),cfg,blocks)
    current=read_json(copy_path)
    assert source.read_bytes()==original,'Original project changed'
    edited=current['segments'][index:index+2]
    assert [(s['start'],s['end']) for s in edited]==plan.pairs
    assert all(s['source_text'] and s['target_text'] for s in edited)
    write_json(destination/'report.json',dict(seconds=time.perf_counter()-started,preview_seconds=preview_seconds,original_unchanged=True,
        boundaries_preserved=True,source_model=cfg['asr']['model_path'],rows=edited,result=result))
    print(json.dumps(dict(report=str(destination/'report.json'),seconds=round(time.perf_counter()-started,2),
        preview_seconds=round(preview_seconds,2),original_unchanged=True,boundaries_preserved=True),ensure_ascii=False))


if __name__=='__main__':
    main()
