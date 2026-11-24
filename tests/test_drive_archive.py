import hashlib
import shutil
import sqlite3
from unittest.mock import Mock

import pytest
from googleapiclient.errors import HttpError
from httplib2 import Response

from digital_signing.drive import GoogleDrive, DOC
from test_contracts import client, HEADERS
from test_workflow import prepared, vendor_payload


def google_workflow(tmp_path):
    _,contract,base,payload = prepared(tmp_path)
    shutil.copyfile(tmp_path/'contracts-demo.sqlite3', tmp_path/'contracts-google.sqlite3')
    with sqlite3.connect(tmp_path/'contracts-google.sqlite3') as con:
        con.execute("UPDATE contracts SET document_id='copied-doc',folder_id='agreement-folder'")
    drive=Mock()
    drive.reserve_pdf_id.return_value='reserved-pdf'
    drive.save_final_pdf.return_value={'id':'reserved-pdf','webViewLink':'https://drive.google.com/file/d/reserved-pdf/view'}
    app=client(tmp_path,'google',lambda settings:drive)
    state=app.post(base+'/1/submit',json=payload,headers=HEADERS).json()
    return app,drive,contract,base,state


def test_final_pdf_is_archived_once_and_exposed_to_workspace(tmp_path):
    app,drive,contract,base,state=google_workflow(tmp_path)
    endpoint=f'/api/contracts/{contract}/submissions/1/drive'
    assert app.post(endpoint,headers=HEADERS).status_code==409
    final=app.post(base+'/1/countersign',json=vendor_payload(state),headers=HEADERS)
    assert final.status_code==200,final.text
    assert final.json()['drive_archive']['status']=='saved'
    args=drive.save_final_pdf.call_args.args
    assert args[0]['document_id']=='copied-doc'
    assert args[1:3]==(1,'reserved-pdf')
    assert args[3]==app.get(final.json()['final_pdf_url']).content
    row=app.get('/api/workspace').json()['contracts'][0]
    assert row['final_drive_url']==drive.save_final_pdf.return_value['webViewLink']
    assert app.post(endpoint).status_code==403
    assert app.post(endpoint,headers=HEADERS).json()['url']==row['final_drive_url']
    assert app.post(base+'/1/countersign',json=vendor_payload(state),headers=HEADERS).status_code==200
    drive.save_final_pdf.assert_called_once()
    drive.reserve_pdf_id.assert_called_once()
    restarted=client(tmp_path,'google',lambda settings:drive)
    assert restarted.get('/api/workspace').json()['contracts'][0]['final_drive_url']==row['final_drive_url']
    deleted=app.request('DELETE',f'/api/contracts/{contract}',json={'confirmation':'Full signing workflow'},headers=HEADERS)
    assert deleted.status_code==200
    drive.delete_archived_pdf.assert_called_once_with('reserved-pdf')


def test_upload_failure_preserves_final_pdf_and_retry_reuses_reserved_id(tmp_path):
    app,drive,contract,base,state=google_workflow(tmp_path)
    drive.save_final_pdf.side_effect=TimeoutError('private network information')
    final=app.post(base+'/1/countersign',json=vendor_payload(state),headers=HEADERS).json()
    assert final['state']=='completed'
    assert final['drive_archive']['status']=='failed'
    assert 'private network' not in final['drive_archive']['error']
    assert app.get(final['final_pdf_url']).content.startswith(b'%PDF')
    row=app.get('/api/workspace').json()['contracts'][0]
    assert row['final_drive_url'] is None and row['drive_save_error']
    drive.save_final_pdf.side_effect=None
    retry=app.post(f'/api/contracts/{contract}/submissions/1/drive',headers=HEADERS)
    assert retry.status_code==200
    drive.reserve_pdf_id.assert_called_once()
    assert {call.args[2] for call in drive.save_final_pdf.call_args_list}=={'reserved-pdf'}
    assert app.get('/api/workspace').json()['contracts'][0]['drive_save_error'] is None


def test_copy_link_can_archive_preexisting_completed_agreement(tmp_path):
    app,contract,base,payload=prepared(tmp_path)
    state=app.post(base+'/1/submit',json=payload,headers=HEADERS).json()
    app.post(base+'/1/countersign',json=vendor_payload(state),headers=HEADERS)
    shutil.copyfile(tmp_path/'contracts-demo.sqlite3',tmp_path/'contracts-google.sqlite3')
    drive=Mock()
    drive.reserve_pdf_id.return_value='reserved-pdf'
    drive.save_final_pdf.return_value={'webViewLink':'https://drive.google.com/file/d/reserved-pdf/view'}
    google=client(tmp_path,'google',lambda settings:drive)
    assert google.post(f'/api/contracts/{contract}/submissions/1/drive',headers=HEADERS).status_code==200
    assert google.get('/api/workspace').json()['contracts'][0]['final_drive_url']


def test_upload_uses_current_document_folder_and_exact_pdf_bytes():
    drive=GoogleDrive.__new__(GoogleDrive); drive.api=Mock()
    files=drive.api.files.return_value
    files.get.return_value.execute.side_effect=[HttpError(Response({'status':404}),b'{}'),{'id':'doc','mimeType':DOC,'parents':['current-folder']}]
    files.create.return_value.execute.return_value={'id':'reserved','webViewLink':'https://drive.google.com/file/d/reserved/view'}
    content=b'%PDF-final signed bytes'
    drive.save_final_pdf({'name':'Jane wedding','document_id':'doc'},3,'reserved',content)
    kwargs=files.create.call_args.kwargs
    assert kwargs['body']=={'id':'reserved','name':'Jane wedding - v3 - Fully executed.pdf','mimeType':'application/pdf','parents':['current-folder']}
    assert kwargs['media_body'].getbytes(0,len(content))==content
    assert 'permissions' not in kwargs['body']


def test_uncertain_upload_recovery_checks_existing_bytes_without_duplicate_create():
    drive=GoogleDrive.__new__(GoogleDrive); drive.api=Mock()
    files=drive.api.files.return_value
    content=b'%PDF-final signed bytes'
    saved={'id':'reserved','webViewLink':'https://drive.google.com/file/d/reserved/view','mimeType':'application/pdf','md5Checksum':hashlib.md5(content).hexdigest()}
    files.get.return_value.execute.return_value=saved
    assert drive.save_final_pdf({'name':'Jane wedding','document_id':'doc'},1,'reserved',content)==saved
    files.create.assert_not_called()
    files.get.return_value.execute.return_value={**saved,'md5Checksum':'wrong'}
    with pytest.raises(ValueError): drive.save_final_pdf({'name':'Jane wedding','document_id':'doc'},1,'reserved',content)
    files.create.assert_not_called()
