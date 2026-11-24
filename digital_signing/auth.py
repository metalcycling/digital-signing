"""Google staff login with explicit approval and revocable server-side sessions."""
import base64
import hashlib
import json
import secrets
import time
from urllib.parse import urlencode, urlsplit

import httpx
from fastapi import Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2 import id_token
from starlette.concurrency import run_in_threadpool

SESSION_COOKIE = 'bello_staff'
FLOW_COOKIE = 'bello_login_flow'


def hashed(value):
    return hashlib.sha256(value.encode()).hexdigest()


def safe_next(value):
    if not value or '\\' in value or any(ord(c) < 32 for c in value):
        return '/'
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc or not value.startswith('/') or value.startswith('//'):
        return '/'
    if parsed.path != '/' and not parsed.path.startswith(('/preview/', '/countersign/')):
        return '/'
    return value


def verify_google_token(token, audience):
    return id_token.verify_oauth2_token(token, GoogleRequest(), audience=audience)


async def exchange_code(config, code, verifier, redirect_uri):
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post('https://oauth2.googleapis.com/token', data={
            'client_id':config['client_id'], 'client_secret':config['client_secret'],
            'code':code, 'code_verifier':verifier, 'grant_type':'authorization_code',
            'redirect_uri':redirect_uri,
        })
        response.raise_for_status()
        return response.json()['id_token']


def install_auth(app, db, lock, settings, static):
    secure = settings.app_base_url.startswith('https://')
    callback_url = settings.app_base_url + '/auth/callback'
    with db() as con:
        con.executescript('''
        CREATE TABLE IF NOT EXISTS staff_identities (
            email TEXT PRIMARY KEY, subject TEXT UNIQUE NOT NULL);
        CREATE TABLE IF NOT EXISTS staff_pending_accounts (
            code TEXT PRIMARY KEY, email TEXT NOT NULL, subject TEXT NOT NULL, expires_at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS staff_sessions (
            token_hash TEXT PRIMARY KEY, email TEXT NOT NULL, subject TEXT NOT NULL,
            expires_at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS staff_login_flows (
            state_hash TEXT PRIMARY KEY, browser_hash TEXT NOT NULL, nonce TEXT NOT NULL,
            verifier TEXT NOT NULL, next_path TEXT NOT NULL, expires_at INTEGER NOT NULL);
        ''')

    def approved_emails():
        try:
            values = json.loads((settings.data_dir/'staff-access.json').read_text())
            if not isinstance(values,list) or any(not isinstance(v,str) or '@' not in v for v in values):
                return set()
            return {v.strip().lower() for v in values}
        except (OSError, ValueError):
            return set()

    def configuration():
        try:
            config = json.loads((settings.data_dir/'google-login-client.json').read_text())['web']
            if not all(isinstance(config.get(key),str) and config[key] for key in ('client_id','client_secret')):
                return None
            if callback_url not in config.get('redirect_uris',[]):
                return None
            return config
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def session(request):
        token = request.cookies.get(SESSION_COOKIE,'')
        if not token or len(token)>200:
            return None
        with db() as con:
            row = con.execute('SELECT * FROM staff_sessions WHERE token_hash=?', (hashed(token),)).fetchone()
        if not row or row['expires_at'] <= time.time() or row['email'] not in approved_emails():
            if row:
                with db() as con:
                    con.execute('DELETE FROM staff_sessions WHERE token_hash=?',(hashed(token),))
            return None
        return {'email':row['email'], 'subject':row['subject']}

    def public_path(path):
        return (path in {'/login','/auth/start','/auth/callback','/auth/status','/auth/logout'}
                or path.startswith('/static/') or path.startswith('/sign/') or path.startswith('/api/sign/'))

    def guard(request):
        if settings.auth_mode == 'local':
            # The development bypass must never accept remote connections.
            peer = request.client.host if request.client else ''
            if peer not in {'127.0.0.1','::1','testclient'}:
                return JSONResponse({'detail':'Local development access is restricted to this computer.'},status_code=403)
            return None
        if public_path(request.url.path):
            return None
        staff = session(request)
        if staff:
            request.state.staff = staff
            return None
        if request.url.path.startswith('/api/') or request.method not in {'GET','HEAD'}:
            return JSONResponse({'detail':'Sign in with an approved Google account to access the workspace.'},status_code=401)
        next_path = safe_next(request.url.path + ('?'+request.url.query if request.url.query else ''))
        return RedirectResponse('/login?'+urlencode({'next':next_path}),status_code=303)

    app.state.auth_guard = guard

    @app.get('/login')
    def login_page():
        return FileResponse(static/'login.html')

    @app.get('/auth/status')
    def status(request: Request):
        return {'mode':settings.auth_mode, 'configured':bool(configuration() and approved_emails()),
                'user':session(request) if settings.auth_mode=='google' else None}

    @app.get('/auth/start')
    def start(request: Request, next: str = '/'):
        config = configuration()
        if settings.auth_mode != 'google' or not config or not approved_emails():
            return RedirectResponse('/login?error=setup',status_code=303)
        if request.headers.get('host') != urlsplit(settings.app_base_url).netloc:
            return RedirectResponse(settings.app_base_url+'/auth/start?'+urlencode({'next':safe_next(next)}),status_code=303)
        state, browser, nonce, verifier = [secrets.token_urlsafe(32) for _ in range(4)]
        with lock, db() as con:
            con.execute('DELETE FROM staff_login_flows WHERE expires_at<=?', (int(time.time()),))
            con.execute('DELETE FROM staff_sessions WHERE expires_at<=?', (int(time.time()),))
            con.execute('INSERT INTO staff_login_flows VALUES (?,?,?,?,?,?)',
                        (hashed(state),hashed(browser),nonce,verifier,safe_next(next),int(time.time())+600))
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
        url = 'https://accounts.google.com/o/oauth2/v2/auth?' + urlencode({
            'client_id':config['client_id'], 'redirect_uri':callback_url, 'response_type':'code',
            'scope':'openid email profile', 'state':state, 'nonce':nonce, 'prompt':'select_account',
            'code_challenge':challenge, 'code_challenge_method':'S256',
        })
        response=RedirectResponse(url,status_code=303)
        response.set_cookie(FLOW_COOKIE,browser,max_age=600,httponly=True,secure=secure,samesite='lax',path='/auth')
        return response

    @app.get('/auth/callback')
    async def callback(request: Request):
        params=request.query_params
        state=params.get('state','')
        browser=request.cookies.get(FLOW_COOKIE,'')
        def fail(kind):
            response=RedirectResponse('/login?'+urlencode({'error':kind}),status_code=303)
            response.delete_cookie(FLOW_COOKIE,path='/auth',secure=secure,httponly=True,samesite='lax')
            return response
        if not state or not browser or len(state)>200 or len(browser)>200:
            return fail('expired')
        with lock, db() as con:
            con.execute('BEGIN IMMEDIATE')
            flow=con.execute('SELECT * FROM staff_login_flows WHERE state_hash=?',(hashed(state),)).fetchone()
            if not flow or flow['expires_at'] <= time.time() or not secrets.compare_digest(flow['browser_hash'],hashed(browser)):
                return fail('expired')
            con.execute('DELETE FROM staff_login_flows WHERE state_hash=?',(hashed(state),))
        if params.get('error') or not params.get('code'):
            return fail('cancelled')
        config=configuration()
        if settings.auth_mode != 'google' or not config:
            return fail('setup')
        try:
            token=await exchange_code(config,params['code'],flow['verifier'],callback_url)
            claims=await run_in_threadpool(verify_google_token,token,config['client_id'])
            if not secrets.compare_digest(str(claims.get('nonce','')),flow['nonce']):
                return fail('invalid')
            subject=claims.get('sub')
            email=str(claims.get('email','')).strip().lower()
            # Bind approved Gmail/Workspace emails to Google's immutable account ID on first login.
            authoritative=email.endswith('@gmail.com') or bool(claims.get('hd'))
            if not isinstance(subject,str) or not subject or claims.get('email_verified') is not True or email not in approved_emails():
                return fail('denied')
            session_token=secrets.token_urlsafe(32)
            with lock, db() as con:
                con.execute('BEGIN IMMEDIATE')
                identity=con.execute('SELECT subject FROM staff_identities WHERE email=?',(email,)).fetchone()
                if identity and identity['subject'] != subject:
                    return fail('denied')
                if not identity and not authoritative:
                    code = secrets.token_hex(6).upper()
                    con.execute('DELETE FROM staff_pending_accounts WHERE expires_at<=?',(int(time.time()),))
                    con.execute('INSERT INTO staff_pending_accounts VALUES (?,?,?,?)',(code,email,subject,int(time.time())+86400))
                    response=RedirectResponse('/login?'+urlencode({'error':'approval','code':code}),status_code=303)
                    response.delete_cookie(FLOW_COOKIE,path='/auth',secure=secure,httponly=True,samesite='lax')
                    return response
                con.execute('INSERT OR IGNORE INTO staff_identities VALUES (?,?)',(email,subject))
                # Invalidate this browser's previous session when changing accounts.
                con.execute('DELETE FROM staff_sessions WHERE token_hash=?',(hashed(request.cookies.get(SESSION_COOKIE,'')),))
                con.execute('INSERT INTO staff_sessions VALUES (?,?,?,?)',
                            (hashed(session_token),email,subject,int(time.time())+settings.auth_session_hours*3600))
        except Exception:
            return fail('invalid')
        response=RedirectResponse(flow['next_path'],status_code=303)
        response.set_cookie(SESSION_COOKIE,session_token,max_age=settings.auth_session_hours*3600,
                            httponly=True,secure=secure,samesite='lax',path='/')
        response.delete_cookie(FLOW_COOKIE,path='/auth',secure=secure,httponly=True,samesite='lax')
        return response

    @app.post('/auth/logout')
    def logout(request: Request):
        with lock, db() as con:
            con.execute('DELETE FROM staff_sessions WHERE token_hash=?',(hashed(request.cookies.get(SESSION_COOKIE,'')),))
        response=JSONResponse({'signed_out':True})
        response.delete_cookie(SESSION_COOKIE,path='/',secure=secure,httponly=True,samesite='lax')
        return response
