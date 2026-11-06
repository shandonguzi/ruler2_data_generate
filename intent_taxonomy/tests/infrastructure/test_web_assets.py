from intent_engine.web import app as web


def test_home_versions_changed_assets_and_requires_revalidation(tmp_path,monkeypatch):
    assets=tmp_path/'static'/'js'
    assets.mkdir(parents=True)
    script=assets/'model-pool.js'
    script.write_text('first')
    (tmp_path/'index.html').write_text('<script src="/static/js/model-pool.js" defer></script>')
    monkeypatch.setattr(web,'WEB_DIR',tmp_path)
    first=web.home()
    assert first.headers['cache-control']=='no-cache'
    assert b'/static/js/model-pool.js?v=' in first.body
    assert web.home().body==first.body
    script.write_text('second')
    assert web.home().body!=first.body
