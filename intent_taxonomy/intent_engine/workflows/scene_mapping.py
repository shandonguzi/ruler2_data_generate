"""Associate each existing atomic intent with one complete-catalog S4 choice."""
import copy
import csv
import json
import os
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from pydantic import BaseModel, Field, field_validator
from intent_engine.atomic.review_reuse import digest,model_identity
from intent_engine.infrastructure.llm_client import LocalLLM
from intent_engine.infrastructure.atomic_json import write_json_atomically
from intent_engine.infrastructure.progress import ProgressReporter
from intent_engine.settings.prompt_settings import prompt_ref
from intent_engine.paths import PROJECT_ROOT

PROMPT=prompt_ref('scene_mapping.MAP_PROMPT')
NAME_PROMPT=prompt_ref('scene_mapping.NAME_PROMPT')
SCENES=PROJECT_ROOT/'config/scenes/s4_scenes.csv'


from intent_engine.workflows.atomic_tasks import TaskName

class SceneChoice(TaskName):
    reason: str = Field(min_length=1)
    scene_id: str = Field(min_length=1)


def load_scenes(path=SCENES):
    with Path(path).open(encoding='utf-8-sig',newline='') as stream:
        rows=list(csv.DictReader(stream))
    if not rows or any(not r.get('scene_id') or not r.get('S4') for r in rows) or len({r['scene_id'] for r in rows})!=len(rows):
        raise ValueError('S4目录为空、缺少ID或ID重复')
    return [{k:r.get(k,'') for k in ('scene_id','S1','S2','S3','S4','具体能力','本地化要点')} for r in rows]


def map_choice(llm,atom,scenes):
    allowed={s['scene_id'] for s in scenes}
    class ValidChoice(SceneChoice):
        @field_validator('task_name')
        @classmethod
        def task_title(cls,value):
            value=value.strip()
            if not value or '\n' in value or '\r' in value:raise ValueError('任务名称必须是非空单行名称')
            return value
        @field_validator('scene_id')
        @classmethod
        def in_catalog(cls,value):
            if value not in allowed:raise ValueError('scene_id 不在场景目录中')
            return value
    result=llm.chat_json(PROMPT,json.dumps({'atomic_intent':atom['text'],'scenes':scenes},ensure_ascii=False),schema=ValidChoice,max_tokens=600)
    if result.scene_id not in {s['scene_id'] for s in scenes}:
        raise ValueError('模型返回了场景目录之外的ID')
    return result


def scene_task_rows(scenes, records):
    tasks={scene['scene_id']:[] for scene in scenes}
    seen={scene_id:set() for scene_id in tasks}
    for record in records:
        scene_id=record.get('scene_id')
        text=(record.get('task_name') or '').strip()
        if record.get('status')!='matched' or scene_id not in tasks or not text:
            continue
        if text not in seen[scene_id]:
            seen[scene_id].add(text)
            tasks[scene_id].append(text)
    return [{**scene,'任务':'\n'.join(tasks[scene['scene_id']])} for scene in scenes]


from intent_engine.exporting.query_tasks import query_task_csv

def run_mapping(cfg,source_path,workers=4):
    from intent_engine.atomic.atomic_discovery import read_stage_source
    from intent_engine.workflows.workflow_layout import create_workflow_directory
    from intent_engine.infrastructure.model_requests import USAGE,usage_since
    source=read_stage_source(source_path,'cluster');scenes=load_scenes()
    atoms=[a for a in source['intents'] if a.get('status')=='valid']
    if not atoms:raise ValueError('没有有效原子意图')
    run_id=datetime.now().strftime('%Y%m%d_%H%M%S')+'_'+uuid.uuid4().hex[:8]
    paths=create_workflow_directory(Path(cfg['paths']['reports_dir']),run_id)
    os.environ['INTENT_TRACE_RUN_ID']=run_id
    reporter=ProgressReporter();reporter.plan([{'id':'s4_mapping','label':'原子意图关联 S4 场景'}]);reporter.start('s4_mapping')
    llm=LocalLLM(cfg['llm']);before=USAGE.snapshot()
    old=source.get('scene_mapping',{}).get('records',[])
    checkpoint=Path(source_path).resolve().parent.parent/'process'/'scene_mapping.json'
    if source.get('requested_stage')=='s4_mapping' and checkpoint.is_file():
        saved=json.loads(checkpoint.read_text(encoding='utf-8'))
        if saved.get('run_id')==source['run_id']:old=saved.get('records',old)
    cache={r['fingerprint']:r for r in old if r.get('status')=='matched' and r.get('fingerprint') and r.get('task_name')}
    settings=model_identity(llm,PROMPT,600)
    name_settings=model_identity(llm,NAME_PROMPT,600)
    old_by_id={r['intent_id']:r for r in old if r.get('status')=='matched' and r.get('intent_id')}
    same_catalog=source.get('scene_mapping',{}).get('scenes')==scenes
    records={};data=copy.deepcopy(source)
    data.update(run_id=run_id,job_id=os.environ.get('INTENT_JOB_ID'),status='running',requested_stage='s4_mapping',stage='export',
                source={'run_id':source['run_id'],'path':str(Path(source_path).resolve()),'stage':source.get('stage')},artifacts={})
    data['scene_mapping']={'scenes':scenes,'records':[]}
    def save(snapshot=False):
        data['scene_mapping']['records']=[records[i] for i in sorted(records)]
        data['token_usage']=usage_since(before)
        data['artifacts'].update(report=str(paths['report']),export=str(paths['export']))
        write_json_atomically(paths['report'].parent/'scene_mapping.json',{'run_id':run_id,**data['scene_mapping']})
        if snapshot:write_json_atomically(paths['report'],data)
        write_json_atomically(paths['manifest'],{k:data.get(k) for k in ('run_id','job_id','status','requested_stage','stage','source','artifacts','summary','completed_stages')})
    save(snapshot=True)
    def one(item):
        i,atom=item;key=digest({'version':2,'text':atom['text'],'scenes':scenes,'settings':settings,'name_settings':name_settings})
        record={k:atom.get(k) for k in ('intent_id','row_id','raw_text','text')};record['fingerprint']=key
        if key in cache:return i,{**record,**{k:cache[key][k] for k in ('scene_id','reason','status','task_name')},'cached':True}
        previous=old_by_id.get(atom['intent_id'])
        can_reuse=bool(same_catalog and previous and previous.get('scene_id') in {s['scene_id'] for s in scenes}
                      and all(previous.get(k)==atom.get(k) for k in ('text','row_id','raw_text')))
        try:
            if can_reuse:
                record.update(scene_id=previous['scene_id'],reason=previous.get('reason',''),selection_source='previous_mapping')
                title=llm.chat_json(NAME_PROMPT,json.dumps({'atomic_intent':atom['text']},ensure_ascii=False),schema=TaskName,max_tokens=600)
                record.update(task_name=title.task_name,status='matched')
            else:
                record.update(**map_choice(llm,atom,scenes).model_dump(),status='matched')
        except Exception as e:record.update(status='failed',error=str(e))
        return i,record
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures=[pool.submit(one,item) for item in enumerate(atoms)]
        for future in as_completed(futures):
            i,record=future.result();records[i]=record
            reporter.update(len(records),len(atoms),cached=sum(bool(r.get('cached')) for r in records.values()),failed=sum(r['status']=='failed' for r in records.values()))
            save()
    scene_index={s['scene_id']:s for s in scenes}
    output=paths['manifest'].parent/'results'/'s4_mapping.csv'
    detail=output.with_name('atomic_s4_details.csv')
    with detail.open('w',encoding='utf-8-sig',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=['intent_id','row_id','raw_text','text','task_name','status','scene_id','S1','S2','S3','S4']);writer.writeheader()
        for i in sorted(records):
            r=records[i];writer.writerow({**{k:r.get(k,'') for k in writer.fieldnames},**{k:scene_index.get(r.get('scene_id'),{}).get(k,'') for k in ('S1','S2','S3','S4')}})
    summary=scene_task_rows(scenes,[records[i] for i in sorted(records)])
    with output.open('w',encoding='utf-8-sig',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=['scene_id','S1','S2','S3','S4','任务','具体能力','本地化要点'])
        writer.writeheader();writer.writerows(summary)
    pairs=output.with_name('query_atomic_intents.csv')
    pairs.write_text(query_task_csv([records[i] for i in sorted(records)],source.get('rows')),encoding='utf-8-sig',newline='')
    data['artifacts']['query_atomic_intents']=str(pairs)
    data['scene_mapping']['scene_tasks']=summary
    data['scene_mapping']['details_path']=str(detail)
    data['artifacts'].update(workbook=str(output),s4_mapping=str(output),s4_details=str(detail))
    data['status']='completed_with_errors' if any(r['status']=='failed' for r in records.values()) else 'completed'
    reporter.complete(data['status']);save(snapshot=True);write_json_atomically(paths['export'],data)
    print(json.dumps({'run_id':run_id,'result':str(output)},ensure_ascii=False))
    return data
