"""Automatic signer dates and name derivation independent of document layout."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo


def is_signing_date(field):
    return field['type'] == 'signing_date' or (field['type']=='date' and field['key'].split('.')[-1] in {'signing_date','signed_date','signature_date','singning_date'})


def signing_day(timestamp, zone):
    return datetime.fromisoformat(timestamp).astimezone(ZoneInfo(zone)).date().isoformat()


def normalize_signer(payload, fields, role, timestamp, zone):
    values = dict(payload.values)
    for field in fields:
        if field['role']==role and is_signing_date(field):
            values[field['key']] = signing_day(timestamp,zone)
    name = payload.signer_name
    if not name:
        for key in (f'{role}.name', f'{role}.full_name', f'{role}.signer_name'):
            value = values.get(key)
            if isinstance(value,str) and value.strip():
                name = value.strip(); break
    if not name:
        for field in fields:
            value = values.get(field['key'])
            if field['role']==role and field['type']=='signature' and isinstance(value,dict) and value.get('typed_name'):
                name = value['typed_name']; break
    return payload.model_copy(update={'values':values,'signer_name':name or role.title()})
