"""Contract tests based on MiniMax V2 docs; no credentials or paid calls."""
import io
import json
import urllib.error
from unittest.mock import Mock

from PIL import Image
import pytest
from sprite_gen.gen import minimax_video as mm


@pytest.fixture
def still(tmp_path):
    path = tmp_path / 'still.png'
    Image.new('RGB', (256, 256)).save(path)
    return path


def test_first_last_frames_and_model_limits(still):
    body = mm.build_payload(prompt='walk', image=still, last_frame=still)
    assert body['model'] == 'MiniMax-H3'
    assert [i.get('role') for i in body['content'][1:]] == ['first_frame', 'last_frame']
    assert body['content'][1]['image_url']['url'].startswith('data:image/png;base64,')
    for opts in [dict(model='MiniMax-H3-Max', resolution='2K'), dict(duration=3), dict(ratio='16:9')]:
        with pytest.raises(mm.MiniMaxError):
            mm.build_payload(prompt='walk', image=still, **opts)
    assert mm.build_payload(prompt='clouds', ratio='16:9')['content'] == [{'type': 'text', 'text': 'clouds'}]
    with pytest.raises(mm.MiniMaxError, match='explicit'):
        mm.build_payload(prompt='clouds')


def test_small_sprite_must_be_padded(tmp_path):
    path = tmp_path / 'tiny.png'
    Image.new('RGBA', (32, 32)).save(path)
    with pytest.raises(mm.MiniMaxError, match='pad sprites'):
        mm.build_payload(prompt='walk', image=path)


def test_http_contract_and_credentials_not_in_payload(monkeypatch):
    client = mm.Client(api_key='test-secret')
    requests = []
    responses = [dict(task_id='123'), dict(task=dict(id='123', status='running'))]
    def open_(request, **kwargs):
        requests.append(request)
        return io.BytesIO(json.dumps(responses.pop(0)).encode())
    monkeypatch.setattr(client._opener, 'open', open_)
    assert client.submit(mm.build_payload(prompt='clouds', ratio='16:9')) == '123'
    assert client.query('123')['status'] == 'running'
    assert requests[0].full_url == 'https://api.minimax.io/v2/video_generation'
    assert requests[1].full_url.endswith('/v2/query/video_generation/123')
    assert requests[1].get_method() == 'GET'
    assert requests[0].get_header('Authorization') == 'Bearer test-secret'
    assert b'test-secret' not in requests[0].data


def test_http_failure_does_not_retry_or_leak_body(monkeypatch):
    client = mm.Client(api_key='test-secret')
    request = Mock(side_effect=urllib.error.HTTPError('https://secret-url', 429, 'secret-token', {}, io.BytesIO(b'secret-body')))
    monkeypatch.setattr(client._opener, 'open', request)
    with pytest.raises(mm.MiniMaxError, match='HTTP 429') as error:
        client.submit({})
    assert 'secret' not in str(error.value)
    assert request.call_count == 1


def test_poll_and_download_without_auth_header(tmp_path, monkeypatch):
    client = Mock()
    client.query.side_effect = [dict(status='queued'), dict(status='running'), dict(status='succeeded', content={'url':'https://cdn.example/clip.mp4'})]
    monkeypatch.setattr(mm.time, 'sleep', lambda _: None)
    mp4 = b'\x00\x00\x00\x18ftypisom' + b'\x00' * 12
    opener = Mock()
    opener.open.return_value = io.BytesIO(mp4)
    monkeypatch.setattr(mm.urllib.request, 'build_opener', lambda *args: opener)
    out = tmp_path / 'clip.mp4'
    result = mm.wait_for_video(client, '123', out)
    assert result['bytes'] == len(mp4)
    assert out.read_bytes() == mp4
    assert opener.open.call_args.args == ('https://cdn.example/clip.mp4',)
    client.submit.assert_not_called()


def test_bad_download_preserves_output_and_cleans_temp(tmp_path, monkeypatch):
    out = tmp_path / 'clip.mp4'
    out.write_bytes(b'original')
    opener = Mock()
    opener.open.return_value = io.BytesIO(b'<html>not a movie</html>')
    monkeypatch.setattr(mm.urllib.request, 'build_opener', lambda *args: opener)
    with pytest.raises(mm.MiniMaxError, match='not an MP4'):
        mm.download('https://cdn.example/clip', out, timeout=5)
    assert out.read_bytes() == b'original'
    assert list(tmp_path.iterdir()) == [out]


def test_submit_only_persists_id_without_polling(tmp_path, monkeypatch):
    client = Mock()
    client.submit.return_value = '123'
    monkeypatch.setattr(mm, 'Client', lambda: client)
    report = tmp_path / 'task.json'
    assert mm.run(prompt='clouds', ratio='16:9', submit_only=True, report=report) == 0
    assert json.loads(report.read_text())['task_id'] == '123'
    client.query.assert_not_called()


def test_resume_error_keeps_task_id_and_never_resubmits(tmp_path, monkeypatch):
    client = Mock()
    client.query.side_effect = mm.MiniMaxError('transport failure')
    monkeypatch.setattr(mm, 'Client', lambda: client)
    report = tmp_path / 'task.json'
    with pytest.raises(SystemExit, match='task_id=123'):
        mm.run_resume(task_id='123', out=tmp_path/'clip.mp4', report=report)
    assert json.loads(report.read_text())['task_id'] == '123'
    assert json.loads(report.read_text())['status'] == 'unfinished'
    client.submit.assert_not_called()


@pytest.mark.parametrize('task_id', ['../123', '123?x=1', '', None])
def test_bad_ids_rejected(task_id):
    with pytest.raises(mm.MiniMaxError):
        mm.valid_task_id(task_id)


def test_existing_output_rejected_before_paid_submit(tmp_path, monkeypatch):
    out = tmp_path/'clip.mp4'
    out.touch()
    client = Mock()
    monkeypatch.setattr(mm, 'Client', lambda: client)
    with pytest.raises(SystemExit, match='exists'):
        mm.run(prompt='clouds', ratio='16:9', out=out)
    client.submit.assert_not_called()


def test_timeout_preserves_no_resubmit(tmp_path, monkeypatch):
    client = Mock()
    client.query.return_value = dict(status='running')
    clock = iter([0, 0, 0, 0, 2])
    monkeypatch.setattr(mm.time, 'monotonic', lambda: next(clock))
    monkeypatch.setattr(mm.time, 'sleep', lambda _: None)
    with pytest.raises(mm.MiniMaxError, match='timeout'):
        mm.wait_for_video(client, '123', tmp_path/'clip.mp4', timeout=1)
    client.submit.assert_not_called()


@pytest.mark.parametrize('status', ['failed', 'cancelled'])
def test_terminal_failure_report_is_not_unfinished(status, tmp_path, monkeypatch):
    client = Mock()
    client.query.return_value = dict(status=status)
    monkeypatch.setattr(mm, 'Client', lambda: client)
    report = tmp_path / 'task.json'
    with pytest.raises(SystemExit, match=status):
        mm.run_resume(task_id='123', out=tmp_path/'clip.mp4', report=report)
    assert json.loads(report.read_text())['status'] == status
    client.submit.assert_not_called()


def test_download_has_no_api_credentials_and_refuses_redirects():
    handler = mm.NoRedirect()
    assert handler.redirect_request(None, None, 302, '', {}, 'https://other.example') is None


def test_client_rejects_unsafe_origin():
    for origin in ['http://api.minimax.io', 'https://user:pass@example.com', 'https://example.com?key=secret']:
        with pytest.raises(mm.MiniMaxError):
            mm.Client(api_key='key', base_url=origin)


def test_truncated_mp4_never_publishes(tmp_path, monkeypatch):
    wire = io.BytesIO(b'\x00\x00\x00\x18ftypisom')
    wire.headers = {'Content-Length': '1000'}
    opener = Mock()
    opener.open.return_value = wire
    monkeypatch.setattr(mm.urllib.request, 'build_opener', lambda *args: opener)
    with pytest.raises(mm.MiniMaxError, match='truncated'):
        mm.download('https://cdn.example/clip', tmp_path/'clip.mp4', timeout=5)
    assert list(tmp_path.iterdir()) == []
