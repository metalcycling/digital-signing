"""Encrypted, consistent application backups; restore only into a new directory."""
import argparse
from contextlib import contextmanager, closing
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import sqlite3
import tarfile
import tempfile
import uuid

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from googleapiclient.http import MediaFileUpload

from digital_signing.config import Settings
from digital_signing.drive import GoogleDrive, FOLDER, safe_id
from digital_signing.setup_auth import private_json

MAGIC = b'BELLO-BACKUP-1\n'
CHUNK = 1024 * 1024
PRIVATE_FILES = ('google-token.json','gmail-credentials.json','google-login-client.json','staff-access.json','backup-config.json')


def checksum(path, algorithm='sha256'):
    result=hashlib.new(algorithm)
    with path.open('rb') as stream:
        for chunk in iter(lambda:stream.read(CHUNK),b''): result.update(chunk)
    return result.hexdigest()


@contextmanager
def backup_lock(data_dir):
    data_dir.mkdir(parents=True,exist_ok=True,mode=0o700)
    with (data_dir/'backup.lock').open('a') as handle:
        try: fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError: raise ValueError('Another backup operation is already running.')
        yield


def read_key(path):
    key=path.read_bytes()
    if len(key)!=32: raise ValueError('The backup key must contain exactly 32 bytes.')
    return key


def encrypt(source, destination, key):
    nonce=secrets.token_bytes(12)
    ctx=Cipher(algorithms.AES(key),modes.GCM(nonce)).encryptor()
    ctx.authenticate_additional_data(MAGIC)
    with source.open('rb') as src, destination.open('xb') as dst:
        os.chmod(destination,0o600)
        dst.write(MAGIC+nonce)
        for chunk in iter(lambda:src.read(CHUNK),b''): dst.write(ctx.update(chunk))
        dst.write(ctx.finalize());dst.write(ctx.tag)


def decrypt(source, destination, key):
    size=source.stat().st_size
    with source.open('rb') as src, destination.open('xb') as dst:
        os.chmod(destination,0o600)
        if src.read(len(MAGIC))!=MAGIC or size<len(MAGIC)+28:
            raise ValueError('Not a supported backup file.')
        nonce=src.read(12);src.seek(-16,2);tag=src.read(16);src.seek(len(MAGIC)+12)
        ctx=Cipher(algorithms.AES(key),modes.GCM(nonce,tag)).decryptor()
        ctx.authenticate_additional_data(MAGIC)
        remaining=size-len(MAGIC)-28
        while remaining:
            chunk=src.read(min(CHUNK,remaining))
            if not chunk: raise ValueError('Incomplete backup file.')
            dst.write(ctx.update(chunk));remaining-=len(chunk)
        # No archive member is inspected or extracted until authentication succeeds.
        dst.write(ctx.finalize())


def snapshot_database(source, target):
    with closing(sqlite3.connect(source.resolve().as_uri()+'?mode=ro',uri=True)) as src:
        with closing(sqlite3.connect(target)) as dst:
            src.backup(dst,pages=256)
            if dst.execute('PRAGMA integrity_check').fetchall()!=[('ok',)]:
                raise ValueError('SQLite integrity check failed.')
    os.chmod(target,0o600)


def create_archive(settings, work):
    database=f'contracts-{settings.mode}.sqlite3'
    snapshot_database(settings.data_dir/database, work/database)
    names=[database]
    for name in PRIVATE_FILES:
        source=settings.data_dir/name
        if source.exists():
            content=source.read_bytes()
            json.loads(content)  # Never publish an incompletely written credential file.
            (work/name).write_bytes(content);os.chmod(work/name,0o600);names.append(name)
    config=settings.model_dump(mode='json')
    (work/'runtime-settings.json').write_text(json.dumps(config,indent=2))
    names.append('runtime-settings.json')
    manifest={'format':1,'created_at':datetime.now(timezone.utc).isoformat(),'mode':settings.mode,
              'files':{name:{'sha256':checksum(work/name),'size':(work/name).stat().st_size} for name in names}}
    (work/'manifest.json').write_text(json.dumps(manifest,indent=2))
    archive=work/'payload.tar.gz'
    with tarfile.open(archive,'w:gz') as tar:
        for name in names+['manifest.json']: tar.add(work/name,arcname=name,recursive=False)
    return archive


def restore(source, key_path, destination):
    destination=destination.resolve()
    if destination.exists(): raise ValueError('Restore destination must not exist; live data is never overwritten.')
    destination.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.bello-restore-',dir=destination.parent) as temporary:
        work=Path(temporary);archive=work/'payload.tar.gz';output=work/'restored';output.mkdir(mode=0o700)
        decrypt(source,archive,read_key(key_path))
        with tarfile.open(archive,'r:gz') as tar:
            members=tar.getmembers()
            allowed=set(PRIVATE_FILES)|{'manifest.json','runtime-settings.json','contracts-demo.sqlite3','contracts-google.sqlite3'}
            if any(not m.isfile() or m.name not in allowed for m in members) or len({m.name for m in members})!=len(members):
                raise ValueError('Unexpected archive entries.')
            member=tar.getmember('manifest.json')
            if member.size>65536: raise ValueError('Invalid manifest.')
            manifest=json.load(tar.extractfile(member))
            if manifest.get('format')!=1 or manifest.get('mode') not in {'google','demo'}:
                raise ValueError('Unsupported backup manifest.')
            if set(manifest['files'])!={m.name for m in members}-{'manifest.json'}:
                raise ValueError('Manifest does not match the archive.')
            for member in members:
                with tar.extractfile(member) as src, (output/member.name).open('xb') as dst: shutil.copyfileobj(src,dst,CHUNK)
                os.chmod(output/member.name,0o600)
            for name,info in manifest['files'].items():
                if (output/name).stat().st_size!=info['size'] or checksum(output/name)!=info['sha256']:
                    raise ValueError('Backup checksum verification failed.')
        database=output/f"contracts-{manifest['mode']}.sqlite3"
        if not database.is_file(): raise ValueError('Backup database is missing.')
        with closing(sqlite3.connect(database)) as con:
            if con.execute('PRAGMA integrity_check').fetchall()!=[('ok',)]: raise ValueError('Restored database is invalid.')
            # A recovered app starts with fresh staff sessions, not old login cookies.
            tables={r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for name in ('staff_sessions','staff_login_flows','staff_pending_accounts'):
                if name in tables: con.execute(f'DELETE FROM {name}')
            con.commit()
        if destination.exists(): raise ValueError('Restore destination appeared while restoring.')
        output.rename(destination)
    return manifest


def initialize(settings, folder_id=None, keep=30):
    if keep<2: raise ValueError("Keep at least two backup generations.")
    with backup_lock(settings.data_dir):
        key=settings.data_dir/'backup.key'
        if not key.exists():
            fd=os.open(key,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(fd,'wb') as stream: stream.write(secrets.token_bytes(32))
        read_key(key)
        config_path=settings.data_dir/'backup-config.json'
        if config_path.exists():
            config=json.loads(config_path.read_text())
            if folder_id and folder_id!=config['folder_id']: raise ValueError('A different backup folder is already configured.')
            config['keep']=keep;private_json(config_path,config);return config
        drive=GoogleDrive(settings)
        parent=safe_id(folder_id or settings.contracts_folder_id)
        metadata=drive.api.files().get(fileId=parent,fields='id,mimeType,trashed',supportsAllDrives=True).execute()
        if metadata.get('trashed') or metadata['mimeType']!=FOLDER: raise ValueError('Choose an active Drive folder.')
        folder=parent if folder_id else drive.folder(parent,'Application Backups')
        config={'folder_id':folder,'namespace':str(uuid.uuid4()),'keep':keep}
        private_json(config_path,config)
        return config


def upload(settings, encrypted, config, api):
    folder=safe_id(config['folder_id'])
    namespace=str(uuid.UUID(config['namespace']))
    keep=int(config['keep'])
    if keep<2: raise ValueError('Keep at least two backup generations.')
    file_id=api.files().generateIds(count=1,space='drive',type='files').execute()['ids'][0]
    metadata=api.files().create(body={'id':file_id,'name':encrypted.name,'parents':[folder],
        'appProperties':{'bello_backup':namespace,'mode':settings.mode}},
        media_body=MediaFileUpload(str(encrypted),mimetype='application/octet-stream',resumable=True),
        fields='id',supportsAllDrives=True).execute()
    remote=api.files().get(fileId=metadata['id'],fields='id,size,md5Checksum',supportsAllDrives=True).execute()
    if int(remote.get('size',-1))!=encrypted.stat().st_size or remote.get('md5Checksum')!=checksum(encrypted,'md5'):
        raise ValueError('Uploaded backup verification failed; retention cleanup was skipped.')
    # Retention is scoped to this installation and mode, and only runs after verification.
    items=[];page=None
    while True:
        result=api.files().list(q=f"'{folder}' in parents and trashed=false and appProperties has {{ key='bello_backup' and value='{namespace}' }} and appProperties has {{ key='mode' and value='{settings.mode}' }}",
            fields='nextPageToken,files(id,createdTime)',orderBy='createdTime desc',pageSize=100,pageToken=page,
            supportsAllDrives=True,includeItemsFromAllDrives=True).execute()
        items.extend(result.get('files',[]));page=result.get('nextPageToken')
        if not page: break
    # Always keep the just-verified upload, even if Drive's listing lags behind creation.
    older=[item for item in items if item['id']!=remote['id']]
    for item in older[keep-1:]:
        api.files().update(fileId=item['id'],body={'trashed':True},supportsAllDrives=True).execute()
    return remote['id']


def run_backup(settings):
    with backup_lock(settings.data_dir):
        status_path=settings.data_dir/'backup-status.json'
        try:
            config=json.loads((settings.data_dir/'backup-config.json').read_text())
            key=read_key(settings.data_dir/'backup.key')
            drive=GoogleDrive(settings)
            with tempfile.TemporaryDirectory(prefix='.backup-',dir=settings.data_dir) as temporary:
                work=Path(temporary);archive=create_archive(settings,work)
                name=f"bello-{settings.mode}-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{secrets.token_hex(4)}.backup"
                encrypted=work/name;encrypt(archive,encrypted,key)
                # Prove the encrypted archive can be restored before publishing it.
                restore(encrypted,settings.data_dir/'backup.key',work/'verified')
                file_id=upload(settings,encrypted,config,drive.api)
            private_json(status_path,{'status':'ok','finished_at':datetime.now(timezone.utc).isoformat(),'file_id':file_id})
            return file_id
        except Exception:
            private_json(status_path,{'status':'failed','finished_at':datetime.now(timezone.utc).isoformat()})
            raise


def create_local(settings, destination):
    with backup_lock(settings.data_dir):
        with tempfile.TemporaryDirectory(prefix='.backup-',dir=settings.data_dir) as temporary:
            work=Path(temporary);key_path=settings.data_dir/'backup.key'
            encrypted=work/'archive.backup'
            encrypt(create_archive(settings,work),encrypted,read_key(key_path))
            restore(encrypted,key_path,work/'verified')
            with encrypted.open('rb') as src, destination.open('xb') as dst:
                os.chmod(destination,0o600);shutil.copyfileobj(src,dst,CHUNK)


def main():
    os.umask(0o077)
    parser=argparse.ArgumentParser(description=__doc__)
    commands=parser.add_subparsers(dest='command',required=True)
    init=commands.add_parser('init');init.add_argument('--folder-id');init.add_argument('--keep',type=int,default=30)
    commands.add_parser('run');commands.add_parser('status')
    local=commands.add_parser('create');local.add_argument('--output',type=Path,required=True)
    recovery=commands.add_parser('restore');recovery.add_argument('file',type=Path);recovery.add_argument('--key',type=Path,required=True);recovery.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();settings=Settings()
    try:
        if args.command=='init':
            config=initialize(settings,args.folder_id,args.keep)
            print('Backup folder: https://drive.google.com/drive/folders/'+config['folder_id'])
            print('Encryption key: '+str(settings.data_dir/'backup.key')+' — keep a separate secure copy; it is never uploaded.')
        elif args.command=='create':
            create_local(settings,args.output);print('Encrypted local backup verified: '+str(args.output))
        elif args.command=='run': print('Backup uploaded and verified: '+run_backup(settings))
        elif args.command=='status': print((settings.data_dir/'backup-status.json').read_text())
        else:
            restore(args.file,args.key,args.output);print('Verified restore saved to '+str(args.output))
    except Exception as error:
        # Provider errors can contain URLs/tokens. Keep cron logs free of exception details.
        print('Backup operation failed ('+type(error).__name__+'). Check configuration, key, disk space, and Google access.')
        raise SystemExit(1)


if __name__=='__main__': main()
