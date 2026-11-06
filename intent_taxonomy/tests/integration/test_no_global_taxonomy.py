from pathlib import Path

import pytest


def test_current_config_and_runtime_do_not_require_global_taxonomy(tmp_path,monkeypatch):
    from intent_engine.settings.config import load_config
    from intent_engine.paths import PROJECT_ROOT
    import intent_engine.workflows.pipeline as pipeline
    config=load_config(PROJECT_ROOT/'config/config.yaml')
    assert 'taxonomy' not in config['paths']
    config['paths']={'reports_dir':str(tmp_path/'reports'),'checkpoint_dir':str(tmp_path/'checkpoints')}
    monkeypatch.setattr(pipeline,'LocalLLM',lambda cfg:None)
    worker=pipeline.IterativePipeline(config)
    assert not hasattr(worker,'taxonomy_path')
    assert not (tmp_path/'taxonomy').exists()


def test_bootstrap_creates_only_workflow_directories(tmp_path,monkeypatch):
    import intent_engine.entrypoints.bootstrap as bootstrap
    import intent_engine.paths as paths
    monkeypatch.setattr(paths,'PROJECT_ROOT',tmp_path)
    monkeypatch.setattr(bootstrap,'load_config',lambda path:{'paths':{'reports_dir':'data/reports','checkpoint_dir':'data/checkpoints'}})
    bootstrap.main()
    assert (tmp_path/'data/reports').is_dir()
    assert not (tmp_path/'data/taxonomy').exists()


def test_status_and_routes_are_workflow_scoped(tmp_path,monkeypatch):
    import intent_engine.web.app as web
    monkeypatch.setattr(web,'_cfg',lambda:{'llm':{'model':'test'}})
    monkeypatch.setattr(web,'_reports_dir',lambda:tmp_path)
    monkeypatch.setattr(web,'_list_reports',lambda:[])
    assert web.api_status()['workflows']['count']==0
    routes={r.path for r in web.app.routes}
    assert not routes & {'/api/taxonomy','/api/taxonomy/manage','/api/taxonomy/manage/apply','/api/jobs/iterate','/api/jobs/summarize'}
    assert '/api/workflows/{run_id}/taxonomy/{artifact}' in routes


@pytest.mark.parametrize('command',['iterate','show-taxonomy','summarize-taxonomy','canonicalize-taxonomy'])
def test_old_global_commands_are_removed(command):
    from intent_engine.entrypoints.cli import build_parser
    with pytest.raises(SystemExit):
        build_parser().parse_args([command])
