import json
import sqlite3
from datetime import datetime, timezone
from io import BytesIO

import pdfplumber

from digital_signing.rendering import validate_values, field_definition
from digital_signing.completed_pdf import display_answer
from test_workflow import prepared
from test_contracts import HEADERS
from test_signatures import signature


def test_signing_dates_use_server_day_and_no_extra_signer_name(tmp_path, monkeypatch):
    app, _, base, payload = prepared(tmp_path)
    with sqlite3.connect(tmp_path/'contracts-demo.sqlite3') as con:
        snapshot=json.loads(con.execute('SELECT snapshot FROM renders').fetchone()[0])
        snapshot['fields'] += [
            {'key':'client.signing_date','type':'date','role':'client','label':'Client signing date','required':True,'options':[]},
            {'key':'vendor.singning_date','type':'date','role':'vendor','label':'Vendor signing date','required':True,'options':[]},
            {'key':'vendor.name','type':'text','role':'vendor','label':'Vendor name','required':True,'options':[]},
        ]
        con.execute('UPDATE renders SET snapshot=?',(json.dumps(snapshot),))
    class Clock(datetime):
        instant=datetime(2026,11,18,2,0,tzinfo=timezone.utc)
        @classmethod
        def now(cls,tz=None): return cls.instant
    monkeypatch.setattr('digital_signing.workflow.datetime',Clock)
    payload.pop('signer_name')
    payload['values']['client.signing_date']='1900-01-01'
    first=app.post(base+'/1/submit',json=payload,headers=HEADERS)
    assert first.status_code==200,first.text
    client=first.json()['client']
    assert client['signer_name']=='Jane Smith'
    assert client['values']['client.signing_date']=='2026-11-17'
    Clock.instant=datetime(2026,11,20,15,0,tzinfo=timezone.utc)
    assert app.post(base+'/1/submit',json=payload,headers=HEADERS).json()['client']==client
    signed=app.post(base+'/1/countersign',json={'consent':True,'values':{'vendor.signature':signature(),'vendor.name':'Vendor Example','vendor.singning_date':'2100-01-01'}},headers=HEADERS)
    assert signed.status_code==200,signed.text
    state=signed.json()
    assert state['vendor']['signer_name']=='Vendor Example'
    assert state['vendor']['values']['vendor.singning_date']=='2026-11-20'
    assert state['client']==client
    with pdfplumber.open(BytesIO(app.get(state['final_pdf_url']).content)) as pdf:
        text='\n'.join(p.extract_text() or '' for p in pdf.pages)
        assert '11-17-2026' in text and '11-20-2026' in text
        assert '2:30 PM' in text


def test_quarter_hour_times_and_explicit_signing_date_type():
    field=field_definition('{{event.start|time}}')
    assert not validate_values([field],{'event.start':'00:00'})
    assert not validate_values([field],{'event.start':'23:45'})
    assert validate_values([field],{'event.start':'12:01'})
    assert validate_values([field],{'event.start':'12:15:01'})
    assert display_answer(field,'00:00')=='12:00 AM'
    assert display_answer(field,'12:00')=='12:00 PM'
    automatic=field_definition('{{vendor.signed|signing_date}}')
    assert automatic['type']=='signing_date'
