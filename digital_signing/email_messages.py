"""Reusable plain-text email copy and local delivery settings."""
import hashlib
import html
import re
import json
import secrets
from datetime import datetime, timedelta

from pydantic import BaseModel, ConfigDict, Field, field_validator


class EmailOptions(BaseModel):
    model_config = ConfigDict(extra='forbid')
    invitation_title: str = Field(default='{contract_name}', min_length=1, max_length=200)
    invitation_body: str = Field(default='Hello {recipient_name},\n\nPlease review and sign the agreement for {invitation_subject} by clicking the button below. If you have any questions, please contact us before signing.\n\nThank you!\nBello Entertainment', min_length=1, max_length=10000)
    completion_body: str = Field(default='Hello {recipient_name},\n\nYour agreement for {contract_name} is fully executed. The final PDF, including both signatures, is attached for your records.\n\nThank you for choosing us. We look forward to working with you!\nBello Entertainment', min_length=1, max_length=10000)
    signing_days: int = Field(default=7, ge=1, le=365, strict=True)
    completion_days: int = Field(default=7, ge=1, le=365, strict=True)

    @field_validator('invitation_body')
    @classmethod
    def invitation_personalization(cls, value):
        # Saved invitation templates now follow the editable subject title.
        return value.replace('{contract_name}', '{invitation_subject}').replace('using the private link below.', 'by clicking the button below.')

    @field_validator('completion_body')
    @classmethod
    def concise_completion(cls, value):
        # Update the former standard paragraph in saved templates as well.
        return value.replace('Your agreement for {contract_name} is now fully executed. The fully executed PDF is attached. You can also download it using the private link below. Please keep a copy for your records.', 'Your agreement for {contract_name} is fully executed. The final PDF, including both signatures, is attached for your records.')

    @field_validator('invitation_title')
    @classmethod
    def subject_title(cls, value):
        if not value.strip() or any(ord(char)<32 or ord(char)==127 for char in value):
            raise ValueError('Enter a subject title without line breaks or control characters.')
        return value.strip()

    @field_validator('invitation_body', 'completion_body')
    @classmethod
    def nonempty(cls, value):
        if not value.strip():
            raise ValueError('Enter an email message.')
        return value.strip()


def defaults(con):
    row = con.execute('SELECT options FROM email_defaults WHERE id=1').fetchone()
    options = EmailOptions.model_validate_json(row[0]) if row else EmailOptions()
    for key in ('invitation_body','completion_body'):
        value = re.sub(r'\bcontract(s)?\b', lambda m: ('Agreement' if m[0][0].isupper() else 'agreement') + ('s' if m[1] else ''), getattr(options,key), flags=re.IGNORECASE)
        value = re.sub('fully signed', 'fully executed', value, flags=re.IGNORECASE)
        setattr(options,key,value)
    return options


def message(template, name, contract, link, days, subject=None, *, include_link=True):
    # Substitute only supported names, preserving other braces as ordinary text.
    import re
    text = re.sub(r'\{(recipient_name|contract_name|invitation_subject)\}', lambda m: {'recipient_name':name,'contract_name':contract,'invitation_subject':subject if subject is not None else contract}[m[1]], template)
    if not include_link:
        return text
    return f'{text}\n\n{link}\n\nThis private link expires in {days} days. Keep it private; anyone with the link can access this agreement.'


def agreement_link(link, label='Open agreement'):
    return f'<p><a href="{html.escape(link, quote=True)}" style="display:inline-block;padding:16px 28px;background-color:#172d39;color:#ffffff;font-family:Arial,sans-serif;font-size:22px;font-weight:bold;line-height:1.4;text-decoration:underline;border-radius:6px;">{html.escape(label)}</a></p>'


def html_message(template, name, contract, link, days, subject=None, *, include_link=True):
    parts = re.split(r'(\{(?:recipient_name|contract_name|invitation_subject)\})', template)
    body = ''.join('<strong>'+html.escape(contract)+'</strong>' if part == '{contract_name}'
                   else '<strong>'+html.escape(subject if subject is not None else contract)+'</strong>' if part == '{invitation_subject}'
                   else html.escape(name) if part == '{recipient_name}' else html.escape(part)
                   for part in parts).replace('\n', '<br>')
    if not include_link:
        return f'<p>{body}</p>'
    return f'<p>{body}</p>{agreement_link(link)}<p>This private link expires in <strong>{days} days</strong>. Keep it private; anyone with the link can access this agreement.</p>'


def queue_completion(con, contract_id, version, contract_name, signed_at, vendor_email):
    invite = con.execute('SELECT * FROM invitations WHERE contract_id=? AND version=?', (contract_id,version)).fetchone()
    link = f'/countersign/{contract_id}?version={version}'
    if invite:
        options = EmailOptions.model_validate_json(invite['email_options']) if invite['email_options'] else EmailOptions()
        token = secrets.token_urlsafe(32)
        link = f'/sign/{token}'
        expires = (datetime.fromisoformat(signed_at)+timedelta(days=options.completion_days)).isoformat()
        con.execute('UPDATE invitations SET completion_token_hash=?, completion_expires_at=? WHERE contract_id=? AND version=?',
                    (hashlib.sha256(token.encode()).hexdigest(), expires, contract_id, version))
    subject = f'Fully executed: {contract_name}'
    vendor_body = f'Hello,\n\nThe agreement for {contract_name} is fully executed. The final PDF, including both signatures, is attached for your records.\n\nThank you,\nBello Entertainment'
    vendor_html = f'<p>Hello,</p><p>The agreement for <strong>{html.escape(contract_name)}</strong> is fully executed. The final PDF, including both signatures, is attached for your records.</p><p>Thank you,<br>Bello Entertainment</p>'
    con.execute('INSERT INTO outbox (contract_id,version,kind,recipient_email,subject,body,link,created_at,html_body,attach_final_pdf) VALUES (?,?,?,?,?,?,?,?,?,1)',
                (contract_id,version,'completed_vendor',vendor_email,subject,vendor_body,link,signed_at,vendor_html))
    if invite:
        body = re.sub('fully signed','fully executed',options.completion_body,flags=re.IGNORECASE)
        con.execute('INSERT INTO outbox (contract_id,version,kind,recipient_email,subject,body,link,created_at,html_body,attach_final_pdf) VALUES (?,?,?,?,?,?,?,?,?,1)',
                    (contract_id,version,'completed',invite['recipient_email'],subject,
                     message(body,invite['recipient_name'],contract_name,link,options.completion_days,include_link=False),link,signed_at,
                     html_message(body,invite['recipient_name'],contract_name,link,options.completion_days,include_link=False)))
