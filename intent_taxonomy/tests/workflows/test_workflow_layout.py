import json
from pathlib import Path
import pytest

from intent_engine.workflows.workflow_layout import create_workflow_directory, workflow_paths
from intent_engine.workflows.workflow_artifacts import load_workflow, list_workflows, workflow_artifact
from intent_engine.workflows.workflow_cleanup import workflow_delete_plan, delete_workflow


def version(reports, run_id, legacy=False, source=None):
    paths = workflow_paths(reports,run_id,legacy=True) if legacy else create_workflow_directory(reports,run_id)
    artifacts = {key:str(paths[key]) for key in ('extract','report','workbook','sentence_results','sentence_workbook')}
    for key,value in artifacts.items():
        Path(value).write_text('{}')
    paths['manifest'].write_text(json.dumps({'run_id':run_id,'artifacts':artifacts,'source':source}))
    return paths


def test_new_and_legacy_list_read_and_scoped_download(tmp_path,monkeypatch):
    import web_server as web
    old=version(tmp_path,'old',legacy=True)
    new=version(tmp_path,'new')
    assert {r['run_id'] for r in list_workflows(tmp_path)} == {'old','new'}
    assert load_workflow(tmp_path,'new')['artifacts']['extract'] == str(new['extract'])
    assert workflow_artifact(tmp_path,'old','extract') == old['extract']
    assert workflow_artifact(tmp_path,'new','sentence_workbook') == new['sentence_workbook']
    monkeypatch.setattr(web,'_reports_dir',lambda:tmp_path)
    listing=web._list_reports()
    assert {f['name'] for f in listing} >= {'new/workflow.json','new/results/sentence_results.xlsx','coldstart_old_workflow.json'}
    assert web._latest('sentence_results.xlsx') == new['sentence_workbook']
    with pytest.raises(FileExistsError):
        create_workflow_directory(tmp_path,'new')
    with pytest.raises(ValueError):
        create_workflow_directory(tmp_path,'../escape')


def test_nested_delete_retains_cross_layout_reference_and_unknown_files(tmp_path):
    parent=version(tmp_path,'parent')
    version(tmp_path,'child',legacy=True,source={'path':str(parent['extract'])})
    notes=parent['manifest'].parent/'notes.txt'
    notes.write_text('keep')
    plan=workflow_delete_plan(tmp_path,'parent')
    assert plan['retained'][0]['name']=='parent/stages/extract.json'
    result=delete_workflow(tmp_path,'parent',plan['revision'])
    assert result['deleted']==5
    assert parent['extract'].exists() and notes.exists()
    assert not parent['manifest'].exists()
    assert not parent['report'].parent.exists()
    assert not parent['sentence_results'].parent.exists()


def test_nested_delete_cleans_empty_dirs_and_rejects_new_reference(tmp_path):
    paths=version(tmp_path,'run')
    plan=workflow_delete_plan(tmp_path,'run')
    child=version(tmp_path,'child',source={'path':str(paths['extract'])})
    with pytest.raises(ValueError,match='重新预览'):
        delete_workflow(tmp_path,'run',plan['revision'])
    newplan=workflow_delete_plan(tmp_path,'child')
    delete_workflow(tmp_path,'child',newplan['revision'])
    assert not child['manifest'].parent.exists()
    plan=workflow_delete_plan(tmp_path,'run')
    delete_workflow(tmp_path,'run',plan['revision'])
    assert not paths['manifest'].parent.exists()


def test_nested_move_failure_restores_layout(tmp_path,monkeypatch):
    import intent_engine.workflows.workflow_cleanup as cleanup
    paths=version(tmp_path,'run')
    plan=workflow_delete_plan(tmp_path,'run')
    original=cleanup.os.replace
    calls=0
    def replace(src,dst):
        nonlocal calls
        calls+=1
        if calls==3:
            raise OSError('simulated')
        original(src,dst)
    monkeypatch.setattr(cleanup.os,'replace',replace)
    with pytest.raises(OSError):
        delete_workflow(tmp_path,'run',plan['revision'])
    assert all(paths[k].exists() for k in ('extract','report','workbook','sentence_results','sentence_workbook','manifest'))
    assert not list(tmp_path.glob('.delete_*'))


def test_nested_directory_symlink_is_not_read_or_deleted(tmp_path):
    reports=tmp_path/'reports';reports.mkdir()
    outside=tmp_path/'outside';outside.mkdir()
    paths=version(outside,'run')
    (reports/'run').symlink_to(paths['manifest'].parent,target_is_directory=True)
    with pytest.raises(ValueError,match='符号链接'):
        load_workflow(reports,'run')
    with pytest.raises(ValueError,match='符号链接'):
        workflow_delete_plan(reports,'run')
    assert list_workflows(reports)==[]
    assert paths['manifest'].exists()
