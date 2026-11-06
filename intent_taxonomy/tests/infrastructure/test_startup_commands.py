"""Validate embedded Python commands without installing any dependencies."""
import importlib.util
import re
import subprocess

import pytest
from intent_engine.paths import PROJECT_ROOT


@pytest.mark.parametrize('script',['start_web.bat','start_web.ps1'])
def test_startup_timing_commands_are_valid_and_preserve_exit_codes(script,monkeypatch,capsys):
    source=(PROJECT_ROOT/'scripts'/script).read_text()
    commands=re.findall(r'-c "([^"]+)"',source)
    assert len(commands)==2
    monkeypatch.setattr(importlib.util,'find_spec',lambda name:object())
    installed=[]
    monkeypatch.setattr(subprocess,'call',lambda command:installed.append(command) or 23)
    for code,expected in zip(commands,[0,23]):
        # A literal percent-format operator can be expanded as a BAT variable.
        assert '%' not in code
        with pytest.raises(SystemExit) as result:
            exec(compile(code,script,'exec'),{})
        assert result.value.code==expected
    assert installed[0][1:]==['-m','pip','install','-r','requirements.txt']
    output=capsys.readouterr().out
    assert '[Startup] Dependency check:' in output
    assert '[Startup] Dependency installation:' in output
