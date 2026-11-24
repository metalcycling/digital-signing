import json

from digital_signing.config import Settings
from digital_signing.deployment_check import check


def configured(tmp_path):
    origin='https://agreements.belloentertainment.com'
    settings=Settings(data_dir=tmp_path,mode='google',email_mode='local',auth_mode='google',app_base_url=origin,email_base_url=origin,template_folder_id='templates',contracts_folder_id='agreements')
    files={'google-token.json':{},'google-login-client.json':{'web':{'client_id':'test','client_secret':'test','redirect_uris':[origin+'/auth/callback']}},'staff-access.json':['staff@gmail.com']}
    for name,data in files.items():
        path=tmp_path/name;path.write_text(json.dumps(data));path.chmod(0o600)
    return settings


def test_preflight_accepts_configured_production_and_checks_callback(tmp_path):
    settings=configured(tmp_path)
    assert check(settings)==[]
    (tmp_path/'google-login-client.json').write_text(json.dumps({'web':{'client_id':'test','client_secret':'test','redirect_uris':['http://localhost:8000/auth/callback']}}))
    assert any('callback' in issue for issue in check(settings))


def test_preflight_rejects_insecure_settings_and_exposed_credentials(tmp_path):
    settings=configured(tmp_path)
    (tmp_path/'google-token.json').chmod(0o644)
    assert any('owner-only' in issue for issue in check(settings))
    settings=settings.model_copy(update={'auth_mode':'local','email_base_url':'http://localhost:8000','email_mode':'gmail'})
    issues=check(settings)
    assert any('authentication' in issue for issue in issues)
    assert any('origins must match' in issue for issue in issues)
    assert any('gmail-credentials' in issue for issue in issues)
