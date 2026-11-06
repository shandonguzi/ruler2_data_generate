"""Exercise entrypoints and real static routing after moving implementations."""
import asyncio
import json
from pathlib import Path
import re
import subprocess
import sys

from intent_engine.paths import PROJECT_ROOT
from intent_engine.web.app import app


async def request(path):
    from urllib.parse import urlsplit
    parsed=urlsplit(path)
    path=parsed.path
    events=[]
    async def receive():
        return {'type':'http.request','body':b'','more_body':False}
    async def send(event):
        events.append(event)
    await app({'type':'http','asgi':{'version':'3.0','spec_version':'2.4'},'method':'GET',
               'scheme':'http','path':path,'raw_path':path.encode(),'query_string':parsed.query.encode(),
               'headers':[],'server':('testserver',80),'client':('127.0.0.1',1234),'root_path':''},receive,send)
    status=next(event['status'] for event in events if event['type']=='http.response.start')
    body=b''.join(event.get('body',b'') for event in events if event['type']=='http.response.body')
    return status,body


def test_real_homepage_declared_assets_are_served_without_network():
    status,html=asyncio.run(request('/'))
    assert status==200
    assets=re.findall(r'(?:src|href)="(/static/[^"#]+)"',html.decode())
    paths=[url.split('?',1)[0] for url in assets]
    assert '/static/js/console.js' in paths and '/static/css/console.css' in paths
    assert '/static/js/model-pool.js' in paths
    for url in assets:
        status,body=asyncio.run(request(url))
        assert status==200 and body, url
    assert asyncio.run(request('/static/js/missing.js'))[0]==404


def test_compatibility_cli_resolves_default_config_outside_project(tmp_path):
    result=subprocess.run([sys.executable,str(PROJECT_ROOT/'main.py'),'discover','--help'],cwd=tmp_path,
                          capture_output=True,text=True,timeout=15)
    assert result.returncode==0,result.stderr
    assert '--iteration-scope' in result.stdout


def test_asgi_compatibility_and_stable_prompt_ids():
    import web_server
    from main import build_parser
    assert web_server.app is app
    assert build_parser().parse_args(['discover','--input','data.csv']).config==str(PROJECT_ROOT/'config'/'config.yaml')
    status,body=asyncio.run(request('/api/prompt-settings'))
    assert status==200
    templates=json.loads(body)['prompts']
    assert any(p['key']=='atomic_discovery.EXTRACT_PROMPT' and p['default'] for p in templates)
