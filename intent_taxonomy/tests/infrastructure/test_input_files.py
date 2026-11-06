from __future__ import annotations

import asyncio
from io import BytesIO

import pandas as pd
import pytest
from fastapi import HTTPException, UploadFile

from intent_engine.infrastructure.excel_io import inspect_input_file, load_excel


@pytest.mark.parametrize('encoding', ['utf-8', 'utf-8-sig', 'gb18030', 'utf-16'])
def test_csv_encoding_and_text_values_are_preserved(tmp_path, encoding):
    path = tmp_path / 'input.csv'
    path.write_text('编号,用户提问,备注\n001,查询天气,NA\n002,,null\n003,"播放音乐,再推荐歌曲",\n', encoding=encoding)
    preview = inspect_input_file(path)
    df = load_excel(str(path), '用户提问')
    assert preview['columns'] == ['编号', '用户提问', '备注']
    assert preview['sheets'] == [] and preview['sheet'] is None
    assert preview['has_rows'] is True
    assert df['编号'].tolist() == ['001', '002', '003']
    assert df['用户提问'].tolist() == ['查询天气', '', '播放音乐,再推荐歌曲']
    assert df['备注'].tolist() == ['NA', 'null', '']
    assert preview['rows'] == df.to_dict(orient='records')


def test_csv_quoted_multiline_and_semicolon_separator(tmp_path):
    path = tmp_path / 'input.csv'
    path.write_text('编号;文本\n01;"第一行\n第二行"\n02;查询天气\n', encoding='utf-8')
    assert load_excel(str(path), '文本')['文本'].tolist() == ['第一行\n第二行', '查询天气']


def test_xlsx_sheet_selection_and_column_names(tmp_path):
    path = tmp_path / 'input.xlsx'
    with pd.ExcelWriter(path) as writer:
        pd.DataFrame({' 用户提问 ': ['first'], '编号': ['001']}).to_excel(writer, sheet_name='训练集', index=False)
        pd.DataFrame({'内容': ['查询天气', '播放音乐'], 100: ['02', '03']}).to_excel(writer, sheet_name='验证集', index=False)
    first = inspect_input_file(path)
    second = inspect_input_file(path, '验证集')
    assert first['sheets'] == ['训练集', '验证集']
    assert first['sheet'] == '训练集'
    assert first['columns'] == ['用户提问', '编号']
    assert second['columns'] == ['内容', '100']
    assert load_excel(str(path), '内容', '验证集')['内容'].tolist() == ['查询天气', '播放音乐']
    assert load_excel(str(path), '100', '验证集')['100'].tolist() == ['02', '03']
    with pytest.raises(ValueError, match='工作表'):
        inspect_input_file(path, '不存在')
    with pytest.raises(ValueError, match='不存在列'):
        load_excel(str(path), '用户提问', '验证集')


def test_preview_is_limited_but_full_input_is_not(tmp_path):
    path = tmp_path / 'input.csv'
    pd.DataFrame({'text': [f'query-{i}' for i in range(10)]}).to_csv(path, index=False)
    assert len(inspect_input_file(path)['rows']) == 5
    assert len(load_excel(str(path), 'text')) == 10


def test_empty_and_ambiguous_headers_are_rejected(tmp_path):
    path = tmp_path / 'input.csv'
    path.write_text('text\n', encoding='utf-8')
    assert inspect_input_file(path)['has_rows'] is False
    with pytest.raises(ValueError, match='没有可分类'):
        load_excel(str(path), 'text')
    path.write_text('text, text \nfirst,second\n', encoding='utf-8')
    with pytest.raises(ValueError, match='重复列名'):
        inspect_input_file(path)


def test_csv_cannot_use_a_stale_excel_sheet(tmp_path):
    path = tmp_path / 'input.csv'
    path.write_text('text\none\n', encoding='utf-8')
    with pytest.raises(ValueError, match='不包含工作表'):
        load_excel(str(path), 'text', 'Sheet1')


@pytest.fixture
def web_input(tmp_path, monkeypatch):
    import web_server as web
    uploads = tmp_path / 'uploads'
    uploads.mkdir()
    monkeypatch.setattr(web, 'ROOT', tmp_path)
    monkeypatch.setattr(web, 'UPLOAD_DIR', uploads)
    monkeypatch.setattr(web, '_cfg', lambda: {'pipeline': {'text_column': '用户提问'}})
    import intent_engine.atomic.atomic_discovery as discovery
    monkeypatch.setattr(discovery,"embedding_settings",lambda cfg:{})
    monkeypatch.setattr(discovery,"check_dependencies",lambda:None)
    commands = []
    monkeypatch.setattr(web, '_start_job', lambda kind, cmd: commands.append((kind, cmd)) or {'job_id': 'test'})
    return web, commands


def test_upload_csv_returns_columns_preview_and_unique_paths(web_input):
    web, _ = web_input
    content = '编号,内容\n001,查询天气\n'.encode('utf-8-sig')
    first = asyncio.run(web.api_upload(UploadFile(filename='data.csv', file=BytesIO(content))))
    second = asyncio.run(web.api_upload(UploadFile(filename='data.csv', file=BytesIO(content))))
    assert first['path'] != second['path']
    assert first['preview']['columns'] == ['编号', '内容']
    assert first['preview']['rows'][0]['编号'] == '001'
    loaded = web.api_input_preview(first['path'])
    assert loaded['columns'] == first['preview']['columns']


def test_upload_xlsx_reads_metadata_and_invalid_upload_is_removed(web_input):
    web, _ = web_input
    buffer = BytesIO()
    with pd.ExcelWriter(buffer) as writer:
        pd.DataFrame({'内容': ['one']}).to_excel(writer, sheet_name='测试表', index=False)
    uploaded = asyncio.run(web.api_upload(UploadFile(filename='input.xlsx', file=BytesIO(buffer.getvalue()))))
    assert uploaded['preview']['sheets'] == ['测试表']
    assert uploaded['preview']['columns'] == ['内容']
    before = set(web.UPLOAD_DIR.iterdir())
    with pytest.raises(HTTPException) as exc:
        asyncio.run(web.api_upload(UploadFile(filename='broken.xlsx', file=BytesIO(b'not-an-excel-file'))))
    assert exc.value.status_code == 400
    assert set(web.UPLOAD_DIR.iterdir()) == before


@pytest.mark.parametrize('kind', ['iterate'])
def test_jobs_pass_the_selected_column_and_sheet_to_cli(web_input, kind):
    web, commands = web_input
    path = web.ROOT / 'input.xlsx'
    with pd.ExcelWriter(path) as writer:
        pd.DataFrame({'用户提问': ['first']}).to_excel(writer, sheet_name='训练集', index=False)
        pd.DataFrame({'内容': ['second']}).to_excel(writer, sheet_name='验证集', index=False)
    cls, endpoint = (web.DiscoveryRequest, web.api_job_discover)
    endpoint(cls(input=str(path), text_column='内容', sheet_name='验证集'))
    command = commands[0][1]
    assert command[command.index('--text-column') + 1] == '内容'
    assert command[command.index('--sheet') + 1] == '验证集'
    with pytest.raises(HTTPException) as exc:
        endpoint(cls(input=str(path), text_column='不存在', sheet_name='验证集'))
    assert exc.value.status_code == 400
    assert len(commands) == 1


@pytest.mark.parametrize('kind', ['iterate'])
def test_csv_job_accepts_custom_column_and_rejects_empty_input(web_input, kind):
    web, commands = web_input
    path = web.ROOT / 'input.csv'
    path.write_text('text,其他\n查询天气,不分类这个值\n', encoding='utf-8')
    cls, endpoint = (web.DiscoveryRequest, web.api_job_discover)
    endpoint(cls(input=str(path), text_column='text'))
    command = commands[0][1]
    assert command[command.index('--text-column') + 1] == 'text'
    assert '--sheet' not in command
    path.write_text('text\n', encoding='utf-8')
    with pytest.raises(HTTPException):
        endpoint(cls(input=str(path), text_column='text'))
    assert len(commands) == 1


def test_cli_exposes_file_column_and_worksheet_options():
    from main import build_parser
    parser = build_parser()
    for command in ['discover']:
        args = parser.parse_args([command, '--input', 'input.xlsx', '--text-column', '内容', '--sheet', '验证集'])
        assert args.sheet_name == '验证集'
        assert args.text_column == '内容'
