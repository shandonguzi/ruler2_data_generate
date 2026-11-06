from pathlib import Path

import pytest
from pydantic import ValidationError


def test_scope_is_available_in_cli_and_validated_in_api():
    from intent_engine.entrypoints.cli import build_parser
    from intent_engine.web.app import DiscoveryRequest, WorkflowStageRequest
    args=build_parser().parse_args(['discover','--input','data.csv','--iteration-scope','unresolved_only'])
    assert args.iteration_scope=='unresolved_only'
    assert DiscoveryRequest(input='data.csv').iteration_scope is None
    with pytest.raises(ValidationError):
        DiscoveryRequest(input='data.csv',iteration_scope='other')
    with pytest.raises(ValidationError):
        WorkflowStageRequest(stage='iterate',iteration_scope='other')


def test_full_workflow_passes_scope_to_worker(monkeypatch):
    import intent_engine.web.app as web
    import intent_engine.atomic.atomic_discovery as discovery
    monkeypatch.setattr(web,'_validate_input_request',lambda req:Path('data.csv'))
    monkeypatch.setattr(web,'_cfg',lambda:{})
    monkeypatch.setattr(discovery,'embedding_settings',lambda cfg:{})
    monkeypatch.setattr(discovery,'check_dependencies',lambda:None)
    monkeypatch.setattr(web,'_start_job',lambda kind,cmd:cmd)
    command=web.api_job_classification_workflow(web.DiscoveryRequest(input='data.csv',iteration_scope='unresolved_only'))
    assert command[command.index('--iteration-scope')+1]=='unresolved_only'


def test_stage_workflow_passes_scope_to_worker(tmp_path,monkeypatch):
    import intent_engine.web.app as web
    import intent_engine.atomic.atomic_discovery as discovery
    import intent_engine.workflows.workflow_artifacts as artifacts
    monkeypatch.setattr(web,'_reports_dir',lambda:tmp_path)
    monkeypatch.setattr(artifacts,'workflow_artifact',lambda *a:tmp_path/'domains.json')
    monkeypatch.setattr(discovery,'read_stage_source',lambda *a:{})
    monkeypatch.setattr(web,'_start_job',lambda kind,cmd:cmd)
    command=web.api_job_workflow_stage(web.WorkflowStageRequest(stage='iterate',run_id='run',artifact='domains',iteration_scope='unresolved_only'))
    assert command[command.index('--iteration-scope')+1]=='unresolved_only'


@pytest.mark.parametrize('value', ['unresolved_only', 'noise_only'])
def test_incremental_scope_normalizes_legacy_config_and_saved_values(value):
    from intent_engine.infrastructure.iteration_scope import normalize_iteration_scope
    assert normalize_iteration_scope(value) == 'unresolved_only'
    assert normalize_iteration_scope('all') == 'all'
    with pytest.raises(ValueError):
        normalize_iteration_scope('invalid')


def test_old_scope_remains_accepted_for_existing_clients():
    from intent_engine.entrypoints.cli import build_parser
    from intent_engine.web.app import DiscoveryRequest, WorkflowStageRequest
    assert build_parser().parse_args(['discover', '--input', 'data.csv', '--iteration-scope', 'noise_only']).iteration_scope == 'noise_only'
    assert DiscoveryRequest(input='data.csv', iteration_scope='noise_only').iteration_scope == 'noise_only'
    assert WorkflowStageRequest(stage='iterate', iteration_scope='noise_only').iteration_scope == 'noise_only'
