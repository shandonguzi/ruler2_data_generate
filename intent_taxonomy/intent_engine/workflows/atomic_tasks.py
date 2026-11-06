"""Independent query/task-name export, without any scene catalog dependency."""
import copy
import json
import os
import uuid
from datetime import datetime
from pathlib import Path
from pydantic import BaseModel,Field,field_validator
from intent_engine.infrastructure.atomic_json import write_json_atomically
from intent_engine.infrastructure.progress import ProgressReporter



class TaskName(BaseModel):
    task_name:str=Field(min_length=1,max_length=160)
    @field_validator('task_name')
    @classmethod
    def single_line(cls,value):
        value=value.strip()
        if not value or '\n' in value or '\r' in value:raise ValueError('任务名称必须为非空单行名称')
        return value


# Public imports retained for existing callers. Export implementation lives together.
from intent_engine.exporting.query_tasks import query_task_csv, saved_task_records


def run_atomic_tasks(cfg,source_path,workers=4):
    from intent_engine.atomic.atomic_discovery import read_stage_source
    from intent_engine.workflows.workflow_layout import create_workflow_directory
    source=read_stage_source(source_path,'cluster');records=saved_task_records(source)
    if not records:raise ValueError('没有有效原子意图')
    reporter=ProgressReporter();reporter.plan([{'id':'atomic_tasks','label':'导出已有原子意图（不请求模型）'}]);reporter.start('atomic_tasks')
    run_id=datetime.now().strftime('%Y%m%d_%H%M%S')+'_'+uuid.uuid4().hex[:8]
    paths=create_workflow_directory(Path(cfg['paths']['reports_dir']),run_id)
    data=copy.deepcopy(source);data.update(run_id=run_id,job_id=os.environ.get('INTENT_JOB_ID'),requested_stage='atomic_tasks',stage='export',status='completed',
        source={'run_id':source['run_id'],'path':str(Path(source_path).resolve()),'stage':source.get('stage')},artifacts={})
    missing=sum(r['status']=='missing_name' for r in records)
    data['atomic_task_names']={'records':records,'missing_names':missing,'mode':'export_saved_only'}
    data['token_usage']={'requests':0,'failed_requests':0,'prompt_tokens':0,'completion_tokens':0,'total_tokens':0,'models':[]}
    output=paths['manifest'].parent/'results'/'query_atomic_intents.csv'
    output.write_text(query_task_csv(records,source.get('rows')),encoding='utf-8-sig',newline='')
    data['artifacts'].update(query_atomic_intents=str(output),workbook=str(output),report=str(paths['report']),export=str(paths['export']))
    write_json_atomically(paths['report'],data);write_json_atomically(paths['export'],data)
    write_json_atomically(paths['manifest'],{k:data.get(k) for k in ('run_id','job_id','status','stage','requested_stage','source','artifacts','summary','completed_stages')})
    reporter.update(len(records),len(records));reporter.complete()
    print(json.dumps({'run_id':run_id,'result':str(output),'missing_names':missing,'model_requests':0},ensure_ascii=False));return data
