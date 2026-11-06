import errno
import json

import pytest

from intent_engine.infrastructure import atomic_json


def test_transient_access_denied_retries_without_exposing_partial_json(tmp_path, monkeypatch):
    target = tmp_path / 'result.json'
    target.write_text('{"old": true}')
    original = atomic_json.os.replace
    attempts, sleeps = [], []
    def replace(source, destination):
        attempts.append(source)
        if len(attempts) <= 2:
            assert json.loads(target.read_text()) == {'old': True}
            assert json.loads(source.read_text()) == {'new': '内容'}
            raise PermissionError(errno.EACCES, 'Access denied', str(destination))
        original(source, destination)
    monkeypatch.setattr(atomic_json.os, 'replace', replace)
    monkeypatch.setattr(atomic_json.time, 'sleep', sleeps.append)
    atomic_json.write_json_atomically(target, {'new': '内容'})
    assert len(attempts) == 3
    assert sleeps == [.05, .1]
    assert json.loads(target.read_text()) == {'new': '内容'}
    assert not list(tmp_path.glob('*.tmp'))


def test_permanent_permission_error_is_bounded_and_preserves_original(tmp_path, monkeypatch):
    target = tmp_path / 'result.json'
    target.write_text('{"old": true}')
    attempts = []
    def deny(*args):
        attempts.append(args)
        raise PermissionError(errno.EACCES, 'Access denied')
    monkeypatch.setattr(atomic_json.os, 'replace', deny)
    monkeypatch.setattr(atomic_json.time, 'sleep', lambda delay: None)
    with pytest.raises(PermissionError):
        atomic_json.write_json_atomically(target, {'new': True})
    assert len(attempts) == 6
    assert json.loads(target.read_text()) == {'old': True}
    assert not list(tmp_path.glob('*.tmp'))


def test_other_io_errors_are_not_retried(tmp_path, monkeypatch):
    def fail(*args):
        raise OSError(errno.ENOSPC, 'No space left on device')
    monkeypatch.setattr(atomic_json.os, 'replace', fail)
    monkeypatch.setattr(atomic_json.time, 'sleep', lambda delay: pytest.fail('Do not retry disk errors'))
    with pytest.raises(OSError) as error:
        atomic_json.write_json_atomically(tmp_path / 'result.json', {})
    assert error.value.errno == errno.ENOSPC


def test_serialization_failure_preserves_original_and_temp_names_are_unique(tmp_path, monkeypatch):
    target = tmp_path / 'state.json'
    target.write_text('{}')
    with pytest.raises(ValueError):
        atomic_json.write_json_atomically(target, {'bad': float('nan')})
    assert target.read_text() == '{}'
    assert not list(tmp_path.glob('*.tmp'))
    sources = []
    original = atomic_json.os.replace
    def track(source, destination):
        sources.append(source)
        original(source, destination)
    monkeypatch.setattr(atomic_json.os, 'replace', track)
    for i in range(2):
        atomic_json.write_json_atomically(target, {'i': i}, sync=True)
    assert sources[0] != sources[1]
