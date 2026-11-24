"""Read-only preflight used by the production service before startup."""
import json
from urllib.parse import urlsplit

from digital_signing.config import Settings


def check(settings):
    issues=[]
    if settings.auth_mode!='google':issues.append('Staff Google authentication must be enabled.')
    parsed=urlsplit(settings.app_base_url)
    if parsed.scheme!='https' or parsed.hostname in {'localhost','127.0.0.1','testserver','::1'}:
        issues.append('Configure a public HTTPS application origin.')
    if settings.email_base_url.rstrip('/')!=settings.app_base_url:
        issues.append('Email and application origins must match.')
    if settings.mode!='google':issues.append('Use Google mode for production.')
    for value in (settings.template_folder_id,settings.contracts_folder_id):
        if not value or value.startswith('REPLACE_'):issues.append('Configure both Drive folder IDs.')
    if settings.template_folder_id==settings.contracts_folder_id:issues.append('Use distinct template and agreement folders.')
    required=['google-token.json','google-login-client.json','staff-access.json']
    if settings.email_mode=='gmail':required.append('gmail-credentials.json')
    for name in required:
        path=settings.data_dir/name
        try:
            data=json.loads(path.read_text())
            if path.stat().st_mode & 0o077:issues.append(f'{name} must have owner-only permissions.')
            if name=='google-login-client.json':
                web=data.get('web',{})
                if not web.get('client_id') or not web.get('client_secret') or settings.app_base_url+'/auth/callback' not in web.get('redirect_uris',[]):
                    issues.append('Install the production Web OAuth credential with the public callback URI.')
            if name=='staff-access.json' and (not isinstance(data,list) or not data or any(not isinstance(e,str) or '@' not in e for e in data)):
                issues.append('Configure the approved staff accounts.')
        except (OSError,ValueError,TypeError,AttributeError):issues.append(f'Missing or invalid {name}.')
    return issues


def main():
    issues=check(Settings())
    if issues:
        for issue in issues:print(issue)
        raise SystemExit(1)
    print('Production configuration preflight passed; live Google access and DNS still require verification.')


if __name__=='__main__':main()
