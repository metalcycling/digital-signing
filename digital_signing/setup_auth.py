"""Configure Google staff login without putting credentials in the project directory."""
import argparse
import json
import os
import re
import sqlite3
import time
from pathlib import Path

from digital_signing.config import Settings


def private_json(path, value):
    temporary=path.with_suffix('.tmp')
    fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
    os.chmod(temporary,0o600)
    with os.fdopen(fd,'w') as stream:
        json.dump(value,stream,indent=2)
        stream.write('\n')
    temporary.replace(path)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--credentials',type=Path,help='Downloaded Google OAuth Web application client JSON.')
    parser.add_argument('--allow',action='append',help='Approved staff email. Repeat to replace the entire allowlist.')
    parser.add_argument('--approve-account',help='One-time code supplied by the person signing in with a non-Workspace email.')
    args=parser.parse_args()
    settings=Settings();settings.prepare()
    if args.credentials:
        document=json.loads(args.credentials.expanduser().read_text())
        web=document.get('web',{})
        if not web.get('client_id') or not web.get('client_secret'):
            parser.error('Choose a Web application OAuth client, not the Desktop client used for Drive.')
        if settings.app_base_url+'/auth/callback' not in web.get('redirect_uris',[]):
            parser.error('Add '+settings.app_base_url+'/auth/callback as an authorized redirect URI, then download the client JSON again.')
        private_json(settings.data_dir/'google-login-client.json',document)
        print('Google login credential saved privately.')
    if args.allow is not None:
        emails=sorted(set(value.strip().lower() for value in args.allow))
        if any(not re.fullmatch(r'[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+',email) for email in emails):
            parser.error('Enter valid email addresses.')
        private_json(settings.data_dir/'staff-access.json',emails)
        print('Approved staff list saved. Removed accounts lose access immediately.')
    if args.approve_account:
        path=settings.data_dir/f'contracts-{settings.mode}.sqlite3'
        if not path.exists():parser.error('Start the app and have the person attempt Google sign-in first.')
        allowed=json.loads((settings.data_dir/'staff-access.json').read_text())
        with sqlite3.connect(path) as con:
            row=con.execute('SELECT email,subject,expires_at FROM staff_pending_accounts WHERE code=?',(args.approve_account.upper(),)).fetchone()
            if not row or row[2]<=time.time() or row[0] not in allowed:
                parser.error('This approval code is invalid, expired, or no longer approved.')
            identity=con.execute('SELECT subject FROM staff_identities WHERE email=?',(row[0],)).fetchone()
            if identity and identity[0]!=row[1]:parser.error('This address is already bound to a different Google account.')
            con.execute('INSERT OR IGNORE INTO staff_identities VALUES (?,?)',row[:2])
            con.execute('DELETE FROM staff_pending_accounts WHERE email=?',(row[0],))
        print('Approved '+row[0]+'. They can now sign in again.')
    if not any((args.credentials,args.allow,args.approve_account)):
        print('Callback URI: '+settings.app_base_url+'/auth/callback')
        print('Login credential installed: '+str((settings.data_dir/'google-login-client.json').exists()))
        print('Approved staff list installed: '+str((settings.data_dir/'staff-access.json').exists()))


if __name__=='__main__':
    main()
