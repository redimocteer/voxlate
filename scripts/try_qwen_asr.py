"""Run a separate Qwen recognition project, without modifying other profiles."""
import argparse
import logging
import os
from pathlib import Path
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from voxlate.common import load_config, read_json, write_json
from voxlate.recognition_models import select_model
from voxlate.installer import resource_root
from voxlate.project_storage import default_project_directory, worker_environment
from voxlate.pipeline import VideoDubPipeline

parser=argparse.ArgumentParser()
parser.add_argument('video',type=Path)
parser.add_argument('--config',type=Path,required=True)
parser.add_argument('--language',choices=['en','ja'],default='en')
args=parser.parse_args()
cfg=load_config(args.config)
select_model(cfg,'asr_qwen_model',resource_root(cfg))
cfg['source_lang']=args.language
directory=default_project_directory(args.video,cfg)
directory.mkdir(parents=True,exist_ok=True)
os.environ.update(worker_environment(directory,dict(os.environ)))
logging.basicConfig(level=logging.INFO,format='%(message)s')
start=time.perf_counter()
VideoDubPipeline(cfg).process(args.video,args.video.with_suffix('.qwen-test.zh.mp4'),directory,stop_after='translate')
write_json(directory/'experiment_metrics.json',{'seconds':time.perf_counter()-start})
print('PROJECT '+str(directory/'project.json'),flush=True)
