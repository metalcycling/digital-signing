from fastapi.testclient import TestClient
from digital_signing.app import create_app
from digital_signing.config import Settings

HEADERS = {"X-Workspace-Request": "1"}


def client(tmp_path, mode="demo", factory=None):
    settings = Settings(auth_mode="local", data_dir=tmp_path, mode=mode, email_mode="local")
    return TestClient(create_app(settings, **({"drive_factory": factory} if factory else {})))


def test_draft_persists_and_duplicate_name_is_rejected(tmp_path):
    app = client(tmp_path)
    payload = {"name": "Jane wedding", "template_id": "demo-harp"}
    response = app.post('/api/contracts', json=payload, headers=HEADERS)
    assert response.status_code == 201
    assert response.json()['document_url'] is None
    payload['name'] = '  JANE wedding  '
    assert app.post('/api/contracts', json=payload, headers=HEADERS).status_code == 409
    assert len(client(tmp_path).get('/api/workspace').json()['contracts']) == 1


def test_invalid_names_templates_and_cross_origin_requests(tmp_path):
    app = client(tmp_path)
    for name in ['ab', '../bad', 'x' * 101, 'bad\nname']:
        assert app.post('/api/contracts', json={'name': name, 'template_id': 'demo-harp'}, headers=HEADERS).status_code == 422
    payload = {'name': 'Jane wedding', 'template_id': 'outside-folder'}
    assert app.post('/api/contracts', json=payload, headers=HEADERS).status_code == 400
    assert app.post('/api/contracts', json=payload).status_code == 403
    assert app.post('/api/contracts', json=payload, headers={**HEADERS, 'Origin': 'https://example.com'}).status_code == 403


def test_google_copy_and_mode_isolation(tmp_path):
    calls = []
    class Drive:
        def __init__(self, settings): pass
        def templates(self): return [{'id': 'real-template', 'name': 'Agreement'}]
        def copy(self, *args):
            calls.append(args)
            return {'id': 'copied-doc', 'webViewLink': 'https://docs.google.com/document/d/copied-doc/edit'}
    app = client(tmp_path, 'google', Drive)
    response = app.post('/api/contracts', json={'name': 'Jane wedding', 'template_id': 'real-template'}, headers=HEADERS)
    assert response.status_code == 201
    assert response.json()['document_id'] == 'copied-doc'
    assert calls[0][0:2] == ('real-template', 'Jane wedding')
    assert client(tmp_path).get('/api/workspace').json()['contracts'] == []


def test_failed_google_copy_does_not_create_draft(tmp_path):
    class Drive:
        def __init__(self, settings): pass
        def templates(self): return [{'id': 'template'}]
        def copy(self, *args): raise RuntimeError('private provider error')
    app = client(tmp_path, 'google', Drive)
    result = app.post('/api/contracts', json={'name': 'Jane wedding', 'template_id': 'template'}, headers=HEADERS)
    assert result.status_code == 502
    assert 'private provider error' not in result.text
    assert app.get('/api/workspace').json()['contracts'] == []


def test_home_and_static_assets(tmp_path):
    app = client(tmp_path)
    assert app.get('/').status_code == 200
    assert app.get('/static/app.js').status_code == 200
    assert app.get('/', headers={'Host': 'untrusted.example'}).status_code == 400


def test_pages_and_stylesheets_keep_their_document_types(tmp_path):
    from pathlib import Path
    import tinycss2
    app = client(tmp_path)
    assert app.get('/').text.lower().startswith('<!doctype html>')
    for page in Path('digital_signing/static').glob('*.html'):
        assert page.read_text().lower().startswith('<!doctype html>'), page
    for name in ['style.css','preview.css']:
        response=app.get('/static/'+name)
        assert response.status_code==200
        assert response.headers['content-type'].startswith('text/css')
        assert '<html' not in response.text.lower()
        rules=tinycss2.parse_stylesheet(response.text,skip_comments=True,skip_whitespace=True)
        assert rules and not any(rule.type=='error' for rule in rules), name
    assert '.workspace-tabs' in app.get('/static/style.css').text
