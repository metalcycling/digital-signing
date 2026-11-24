import sqlite3
from unittest.mock import Mock

import pytest
from googleapiclient.errors import HttpError
from httplib2 import Response

from digital_signing.drive import GoogleDrive
from digital_signing.config import Settings
from test_contracts import client, HEADERS
from test_workflow import prepared, vendor_payload


def delete(app, contract, name='Full signing workflow', headers=HEADERS):
    return app.request('DELETE', '/api/contracts/'+contract, json={'confirmation':name}, headers=headers)


def test_delete_all_versions_signatures_assets_and_notifications(tmp_path):
    app, contract, base, payload = prepared(tmp_path)
    submitted = app.post(base+'/1/submit', json=payload, headers=HEADERS).json()
    final = app.post(base+'/1/countersign', json=vendor_payload(submitted), headers=HEADERS).json()
    assert app.post('/api/contracts/'+contract+'/revisions', headers=HEADERS).status_code == 201
    assert delete(app, contract, headers={}).status_code == 403
    assert delete(app, contract, 'wrong name').status_code == 400
    assert app.get(base+'/1').status_code == 200
    assert delete(app, contract).status_code == 200
    assert client(tmp_path).get('/api/workspace').json()['contracts'] == []
    assert app.get('/api/notifications').json() == []
    for url in [base, base+'/1', base+'/2', base+'/1/pages/1.png', base+'/1/source.pdf',
                submitted['client_pdf_url'], final['final_pdf_url'], '/preview/'+contract, '/countersign/'+contract]:
        assert app.get(url).status_code == 404, url
    with sqlite3.connect(tmp_path/'contracts-demo.sqlite3') as con:
        for table in ['contracts', 'renders', 'render_assets', 'submissions', 'workflow_events']:
            assert con.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] == 0
    assert app.post('/api/contracts',json={'name':'Full signing workflow','template_id':'demo-harp'},headers=HEADERS).status_code == 201


def test_remote_failure_preserves_records_and_retry_remembers_folder(tmp_path):
    drive = Mock()
    drive.templates.return_value = [{'id':'template'}]
    drive.copy.return_value = {'id':'copied-doc'}  # Legacy contract without tracked folder.
    drive.contract_folder.return_value = 'dedicated-folder'
    drive.delete_contract.side_effect = RuntimeError('private error')
    app = client(tmp_path, 'google', lambda settings: drive)
    contract = app.post('/api/contracts', json={'name':'Test deletion','template_id':'template'}, headers=HEADERS).json()['id']
    assert delete(app, contract, 'Test deletion').status_code == 502
    assert len(app.get('/api/workspace').json()['contracts']) == 1
    drive.delete_contract.side_effect = None
    assert delete(app, contract, 'Test deletion').status_code == 200
    assert drive.contract_folder.call_count == 1
    assert drive.delete_contract.call_args.args[1] == 'dedicated-folder'


def test_drive_permanently_deletes_only_contract_targets_and_retries_missing_document():
    drive = GoogleDrive.__new__(GoogleDrive)
    drive.settings = Settings(contracts_folder_id='storage-root',template_folder_id='template-root')
    drive.api = Mock()
    drive.api.files.return_value.delete.return_value.execute.side_effect = [HttpError(Response({'status':404}), b'{}'), {}]
    drive.delete_contract({'document_id':'copied-doc','template_id':'template'}, 'dedicated-folder')
    assert [call.kwargs['fileId'] for call in drive.api.files.return_value.delete.call_args_list] == ['copied-doc','dedicated-folder']
    drive.api.reset_mock()
    with pytest.raises(ValueError):
        drive.delete_contract({'document_id':'template','template_id':'template'}, 'dedicated-folder')
    drive.api.files.assert_not_called()


def test_drive_permission_error_is_not_treated_as_success():
    drive = GoogleDrive.__new__(GoogleDrive)
    drive.settings = Settings(contracts_folder_id='root')
    drive.api = Mock()
    drive.api.files.return_value.delete.return_value.execute.side_effect = HttpError(Response({'status':403}),b'{}')
    with pytest.raises(HttpError):
        drive.delete_contract({'document_id':'doc','template_id':'template'}, 'folder')
    assert drive.api.files.return_value.delete.call_count == 1
