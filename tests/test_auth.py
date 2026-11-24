import json
import sqlite3
import time
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from digital_signing import auth
from digital_signing.app import create_app
from digital_signing.config import Settings
from test_contracts import HEADERS
from test_workflow import prepared, vendor_payload
from test_delivery import invitation


EMAIL='approved@gmail.com'


def secured(tmp_path, base='https://testserver', configured=True):
    if configured:
        (tmp_path/'google-login-client.json').write_text(json.dumps({'web':{'client_id':'test-client','client_secret':'test-secret','redirect_uris':[base+'/auth/callback']}}))
        (tmp_path/'staff-access.json').write_text(json.dumps([EMAIL,'lily@example.com']))
    return TestClient(create_app(Settings(data_dir=tmp_path,mode='demo',email_mode='local',auth_mode='google',app_base_url=base)),base_url=base,follow_redirects=False)


def begin(app, next='/'):
    response=app.get('/auth/start',params={'next':next})
    assert response.status_code==303
    params={key:values[0] for key,values in parse_qs(urlsplit(response.headers['location']).query).items()}
    return response,params


def mock_google(monkeypatch, params, **overrides):
    claims={'sub':'google-account-1','email':EMAIL,'email_verified':True,'nonce':params['nonce'],**overrides}
    async def exchange(config,code,verifier,redirect_uri):
        assert config['client_id']=='test-client'
        assert code=='one-time-code'
        assert len(verifier)>=43
        assert redirect_uri.endswith('/auth/callback')
        return 'signed-google-id-token'
    def verify(token,audience):
        assert token=='signed-google-id-token' and audience=='test-client'
        return claims
    monkeypatch.setattr(auth,'exchange_code',exchange)
    monkeypatch.setattr(auth,'verify_google_token',verify)


def finish(app, params):
    return app.get('/auth/callback',params={'state':params['state'],'code':'one-time-code'})


def login(app,monkeypatch):
    _,params=begin(app)
    mock_google(monkeypatch,params)
    return finish(app,params)


def test_admin_routes_require_login_and_no_sensitive_metadata_leaks(tmp_path):
    app=secured(tmp_path)
    for url in ['/api/workspace','/api/templates','/api/outbox','/api/notifications','/api/email-defaults','/api/email-delivery','/api/contracts/anything/renders','/api/contracts/x/submissions/1/final.pdf']:
        result=app.get(url)
        assert result.status_code==401,(url,result.text)
        assert result.headers['cache-control']=='no-store'
    for url in ['/','/preview/x?version=1','/countersign/x','/docs','/openapi.json']:
        assert app.get(url).headers['location'].startswith('/login?')
    assert app.post('/api/contracts',json={},headers=HEADERS).status_code==401
    assert app.get('/login').status_code==200
    assert app.get('/static/login.js').status_code==200
    assert app.get('/api/sign/unknown').status_code==404


def test_google_login_binds_identity_and_sets_secure_session(tmp_path,monkeypatch):
    app=secured(tmp_path)
    start,params=begin(app,'/countersign/test?version=2')
    assert params['scope']=='openid email profile'
    assert params['code_challenge_method']=='S256'
    assert 'Secure' in start.headers['set-cookie'] and 'HttpOnly' in start.headers['set-cookie']
    mock_google(monkeypatch,params)
    response=finish(app,params)
    assert response.headers['location']=='/countersign/test?version=2'
    cookie=response.headers['set-cookie']
    assert 'Secure' in cookie and 'HttpOnly' in cookie and 'SameSite=lax' in cookie
    assert app.get('/api/workspace').status_code==200
    assert app.get('/auth/status').json()['user']['email']==EMAIL
    token=app.cookies.get(auth.SESSION_COOKIE)
    with sqlite3.connect(tmp_path/'contracts-demo.sqlite3') as con:
        assert con.execute('SELECT token_hash FROM staff_sessions').fetchone()[0]!=token
        assert con.execute('SELECT email,subject FROM staff_identities').fetchone()==(EMAIL,'google-account-1')
        assert con.execute('SELECT count(*) FROM staff_login_flows').fetchone()[0]==0
    assert app.get('/api/workspace').headers['strict-transport-security']=='max-age=31536000'


@pytest.mark.parametrize('claims',[{'email':'outsider@gmail.com'},{'email_verified':False},{'nonce':'wrong'},{'sub':''}])
def test_unapproved_or_invalid_google_claims_cannot_create_session(tmp_path,monkeypatch,claims):
    app=secured(tmp_path);_,params=begin(app);mock_google(monkeypatch,params,**claims)
    assert finish(app,params).headers['location'].startswith('/login?error=')
    assert app.get('/api/workspace').status_code==401


def test_signature_verification_failure_and_forged_cookie_fail_closed(tmp_path,monkeypatch):
    app=secured(tmp_path);_,params=begin(app);mock_google(monkeypatch,params)
    def invalid(*args):raise ValueError('bad signature')
    monkeypatch.setattr(auth,'verify_google_token',invalid)
    assert finish(app,params).headers['location']=='/login?error=invalid'
    app.cookies.set(auth.SESSION_COOKIE,'fabricated-token')
    assert app.get('/api/workspace').status_code==401


def test_state_cookie_expiry_and_replay_are_enforced(tmp_path,monkeypatch):
    app=secured(tmp_path);_,params=begin(app);mock_google(monkeypatch,params)
    stranger=secured(tmp_path)
    assert finish(stranger,params).headers['location']=='/login?error=expired'
    assert finish(app,params).headers['location']=='/'
    assert finish(app,params).headers['location']=='/login?error=expired'
    _,params=begin(app)
    with sqlite3.connect(tmp_path/'contracts-demo.sqlite3') as con:con.execute('UPDATE staff_login_flows SET expires_at=0')
    assert finish(app,params).headers['location']=='/login?error=expired'


def test_logout_revocation_expiry_and_account_binding(tmp_path,monkeypatch):
    app=secured(tmp_path);login(app,monkeypatch)
    assert app.post('/auth/logout').status_code==403
    assert app.post('/auth/logout',headers={**HEADERS,'Origin':'https://evil.example'}).status_code==403
    assert app.get('/api/workspace').status_code==200
    assert app.post('/auth/logout',headers=HEADERS).status_code==200
    assert app.get('/api/workspace').status_code==401
    login(app,monkeypatch)
    (tmp_path/'staff-access.json').write_text('[]')
    assert app.get('/api/workspace').status_code==401
    (tmp_path/'staff-access.json').write_text(json.dumps([EMAIL]))
    login(app,monkeypatch)
    with sqlite3.connect(tmp_path/'contracts-demo.sqlite3') as con:con.execute('UPDATE staff_sessions SET expires_at=0')
    assert app.get('/api/workspace').status_code==401
    _,params=begin(app);mock_google(monkeypatch,params,sub='different-google-account')
    assert finish(app,params).headers['location']=='/login?error=denied'


def test_non_workspace_address_requires_explicit_account_approval(tmp_path,monkeypatch):
    app=secured(tmp_path);_,params=begin(app);mock_google(monkeypatch,params,email='lily@example.com')
    response=finish(app,params)
    assert response.headers['location'].startswith('/login?error=approval&code=')
    assert app.get('/api/workspace').status_code==401
    code=parse_qs(urlsplit(response.headers['location']).query)['code'][0]
    # The CLI is the only approval interface; there is no public approval endpoint.
    from digital_signing import setup_auth
    monkeypatch.setattr(setup_auth,'Settings',lambda:Settings(data_dir=tmp_path,mode='demo'))
    monkeypatch.setattr('sys.argv',['setup_auth','--approve-account',code])
    setup_auth.main()
    _,params=begin(app);mock_google(monkeypatch,params,email='lily@example.com')
    assert finish(app,params).headers['location']=='/'
    assert app.get('/api/workspace').status_code==200


def test_workspace_domain_account_can_bind_on_first_login(tmp_path,monkeypatch):
    app=secured(tmp_path);_,params=begin(app);mock_google(monkeypatch,params,email='lily@example.com',hd='example.com')
    assert finish(app,params).headers['location']=='/'


@pytest.mark.parametrize('next',['https://evil.example','//evil.example','/\\evil.example','/api/outbox','/auth/logout'])
def test_return_destination_cannot_redirect_to_other_sites(tmp_path,monkeypatch,next):
    app=secured(tmp_path);_,params=begin(app,next);mock_google(monkeypatch,params)
    assert finish(app,params).headers['location']=='/'


def test_missing_configuration_locks_workspace_without_breaking_login_page(tmp_path):
    app=secured(tmp_path,configured=False)
    assert app.get('/auth/status').json()['configured'] is False
    assert app.get('/auth/start').headers['location']=='/login?error=setup'
    assert app.get('/api/workspace').status_code==401
    assert app.get('/login').status_code==200


def test_public_client_can_submit_without_staff_session_but_cannot_administer(tmp_path,monkeypatch):
    local,contract,base,payload=prepared(tmp_path)
    email,private=invitation(local,base)
    app=secured(tmp_path)
    assert app.get(email['link']).status_code==200
    assert app.get(private+'/1').status_code==200
    result=app.post(private+'/1/submit',json=payload,headers=HEADERS)
    assert result.status_code==200,result.text
    assert app.get(result.json()['client_pdf_url']).status_code==200
    assert app.post(base+'/1/countersign',json=vendor_payload(result.json()),headers=HEADERS).status_code==401
    assert app.post(private+'/1/countersign',json=payload,headers=HEADERS).status_code==404
    login(app,monkeypatch)
    assert app.post(base+'/1/countersign',json=vendor_payload(result.json()),headers=HEADERS).status_code==200


def test_public_host_requires_https_and_authentication():
    with pytest.raises(ValidationError):Settings(app_base_url='http://agreements.example.com')
    with pytest.raises(ValidationError):Settings(app_base_url='https://agreements.example.com',auth_mode='local')
    for value in ['https://example.com/path','https://user:pass@example.com','https://example.com?x=1']:
        with pytest.raises(ValidationError):Settings(app_base_url=value)


def test_local_mode_rejects_remote_connections(tmp_path):
    app=create_app(Settings(data_dir=tmp_path,mode='demo',email_mode='local',auth_mode='local'))
    remote=TestClient(app,client=('203.0.113.3',12345))
    assert remote.get('/api/workspace').status_code==403
