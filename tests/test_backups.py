import json
from pathlib import Path
import secrets
import sqlite3
import tarfile
from unittest.mock import Mock

import pytest
from cryptography.exceptions import InvalidTag

from digital_signing.backups import (backup_lock, create_archive, encrypt, restore, upload, checksum, run_backup)
from digital_signing.config import Settings


def fixture(tmp_path):
    data=tmp_path/'data';data.mkdir()
    settings=Settings(data_dir=data,mode='google',email_mode='local')
    con=sqlite3.connect(data/'contracts-google.sqlite3')
    con.execute('PRAGMA journal_mode=WAL')
    con.executescript("CREATE TABLE answers (value TEXT); INSERT INTO answers VALUES ('saved answer'); CREATE TABLE staff_sessions (token TEXT); INSERT INTO staff_sessions VALUES ('old cookie');")
    (data/'gmail-credentials.json').write_text(json.dumps({'secret':'test-only-secret'}))
    key=data/'backup.key';key.write_bytes(secrets.token_bytes(32))
    work=tmp_path/'work';work.mkdir()
    return settings,con,key,work


def test_snapshot_includes_committed_wal_and_restore_preserves_original(tmp_path):
    settings,con,key,work=fixture(tmp_path)
    try:
        archive=create_archive(settings,work)
        encrypted=work/'encrypted.backup';encrypt(archive,encrypted,key.read_bytes())
        assert b'test-only-secret' not in encrypted.read_bytes()
        with tarfile.open(archive) as tar:assert 'backup.key' not in tar.getnames()
        output=tmp_path/'restored';restore(encrypted,key,output)
        with sqlite3.connect(output/'contracts-google.sqlite3') as recovered:
            assert recovered.execute('SELECT value FROM answers').fetchone()[0]=='saved answer'
            assert recovered.execute('SELECT COUNT(*) FROM staff_sessions').fetchone()[0]==0
        assert con.execute('SELECT COUNT(*) FROM staff_sessions').fetchone()[0]==1
        assert json.loads((output/'gmail-credentials.json').read_text())=={'secret':'test-only-secret'}
        assert (output/'gmail-credentials.json').stat().st_mode & 0o777==0o600
        with pytest.raises(ValueError):restore(encrypted,key,settings.data_dir)
    finally:con.close()


@pytest.mark.parametrize('damage',['wrong-key','ciphertext','truncated'])
def test_damaged_backup_never_restores_partial_data(tmp_path,damage):
    settings,con,key,work=fixture(tmp_path)
    try:
        encrypted=work/'encrypted.backup';encrypt(create_archive(settings,work),encrypted,key.read_bytes())
        if damage=='wrong-key':key.write_bytes(secrets.token_bytes(32))
        elif damage=='ciphertext':
            content=bytearray(encrypted.read_bytes());content[50]^=1;encrypted.write_bytes(content)
        else:encrypted.write_bytes(encrypted.read_bytes()[:-10])
        output=tmp_path/'restored'
        with pytest.raises((InvalidTag,ValueError)):restore(encrypted,key,output)
        assert not output.exists()
    finally:con.close()


def test_authenticated_archive_still_rejects_unsafe_paths(tmp_path):
    settings,con,key,work=fixture(tmp_path)
    try:
        archive=work/'bad.tar.gz'
        with tarfile.open(archive,'w:gz') as tar:tar.add(key,arcname='../escaped')
        encrypted=work/'bad.backup';encrypt(archive,encrypted,key.read_bytes())
        with pytest.raises(ValueError,match='Unexpected archive'):restore(encrypted,key,tmp_path/'restored')
        assert not (tmp_path/'escaped').exists()
    finally:con.close()


def test_retention_only_after_verified_upload_and_with_scoped_query(tmp_path):
    settings=Settings(data_dir=tmp_path,mode="demo")
    file=tmp_path/'test.backup';file.write_bytes(b'encrypted data')
    config={'folder_id':'dedicated-folder','namespace':'12345678-1234-1234-1234-123456789abc','keep':2}
    api=Mock();files=api.files.return_value
    files.generateIds.return_value.execute.return_value={'ids':['new']}
    files.create.return_value.execute.return_value={'id':'new'}
    files.get.return_value.execute.return_value={'id':'new','size':file.stat().st_size,'md5Checksum':checksum(file,'md5')}
    files.list.return_value.execute.return_value={'files':[{'id':'new'},{'id':'previous'},{'id':'older'}]}
    assert upload(settings,file,config,api)=='new'
    assert config['namespace'] in files.list.call_args.kwargs['q']
    assert "value='demo'" in files.list.call_args.kwargs['q']
    files.update.assert_called_once_with(fileId='older',body={'trashed':True},supportsAllDrives=True)
    files.update.reset_mock();files.list.reset_mock()
    files.get.return_value.execute.return_value={'id':'new','size':1,'md5Checksum':'bad'}
    with pytest.raises(ValueError):upload(settings,file,config,api)
    files.update.assert_not_called();files.list.assert_not_called()


def test_overlapping_backups_are_rejected(tmp_path):
    with backup_lock(tmp_path):
        with pytest.raises(ValueError):
            with backup_lock(tmp_path):pass


def test_missing_source_database_fails_without_creating_an_empty_one(tmp_path):
    work=tmp_path/'work';work.mkdir()
    with pytest.raises(sqlite3.OperationalError):create_archive(Settings(data_dir=tmp_path,mode="demo"),work)
    assert not (tmp_path/'contracts-demo.sqlite3').exists()


def test_backup_failure_records_status_without_leaking_provider_details(tmp_path,monkeypatch):
    settings=Settings(data_dir=tmp_path,mode="demo")
    (tmp_path/'backup-config.json').write_text('{}');(tmp_path/'backup.key').write_bytes(secrets.token_bytes(32))
    def fail(settings):raise RuntimeError('secret provider token')
    monkeypatch.setattr('digital_signing.backups.GoogleDrive',fail)
    with pytest.raises(RuntimeError):run_backup(settings)
    status=(tmp_path/'backup-status.json').read_text()
    assert json.loads(status)['status']=='failed'
    assert 'secret' not in status
