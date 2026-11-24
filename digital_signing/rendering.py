"""Turn a Google HTML export into safe document markup and a field schema.

No source HTML, CSS selectors, scripts or external image URLs are trusted.
"""
import base64
import re
from datetime import date, time
from decimal import Decimal, InvalidOperation
from io import BytesIO
from zipfile import ZipFile, BadZipFile

from bs4 import BeautifulSoup, Comment, NavigableString
import tinycss2
from digital_signing.signatures import signature_error
from digital_signing.signing_rules import is_signing_date

TYPES = {'text', 'textarea', 'number', 'date', 'signing_date', 'time', 'phone', 'email', 'select', 'checkbox', 'initials', 'signature'}
TOKEN = re.compile(r'\{\{[^{}]*\}\}|<[A-Z][A-Z_]*>')
KEY = re.compile(r'[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*\Z')
SAFE_TAGS = {'p', 'div', 'span', 'br', 'hr', 'strong', 'b', 'em', 'i', 'u', 's', 'sup', 'sub', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'ul', 'ol', 'li', 'table', 'thead', 'tbody', 'tfoot', 'tr', 'td', 'th', 'img'}
STYLE_PROPERTIES = {'font-weight', 'font-style', 'font-size', 'font-family', 'text-decoration', 'text-align', 'color', 'background-color', 'vertical-align', 'line-height', 'padding', 'padding-left', 'padding-right', 'padding-top', 'padding-bottom', 'margin', 'margin-left', 'margin-right', 'margin-top', 'margin-bottom', 'border', 'border-width', 'border-style', 'border-color', 'border-collapse', 'width', 'max-width', 'height', 'text-indent', 'page-break-before', 'page-break-after', 'break-before', 'break-after'}


class RenderError(ValueError):
    pass


def clean_style(value):
    safe = []
    for item in tinycss2.parse_declaration_list(value, skip_whitespace=True, skip_comments=True):
        if item.type != 'declaration' or item.lower_name not in STYLE_PROPERTIES:
            continue
        # No functions (url(), expression(), var()), escapes, controls or CSS blocks.
        if any(t.type not in {'ident', 'number', 'dimension', 'percentage', 'hash', 'string', 'whitespace', 'literal'} for t in item.value):
            continue
        text = tinycss2.serialize(item.value)
        if re.search(r'[{}<>\\;\x00-\x1f]', text) or any(t.type == 'literal' and t.value not in {',', '/'} for t in item.value):
            continue
        safe.append(f'{item.lower_name}:{text}')
    return ';'.join(safe)


def unpack_export(content):
    try:
        with ZipFile(BytesIO(content)) as archive:
            members = archive.infolist()
            if len(members) > 500 or sum(m.file_size for m in members) > 30_000_000:
                raise RenderError('Document export is too large to preview (30 MB limit).')
            names = [m.filename for m in members if m.filename.lower().endswith('.html')]
            if len(names) != 1:
                raise RenderError('Expected one HTML document in the Google export.')
            html = archive.read(names[0]).decode('utf-8')
            images = {}
            for member in members:
                if member.filename.startswith('images/') and member.file_size <= 5_000_000:
                    data = archive.read(member)
                    mime = ('image/png' if data.startswith(b'\x89PNG\r\n\x1a\n') else
                            'image/jpeg' if data.startswith(b'\xff\xd8\xff') else
                            'image/gif' if data.startswith((b'GIF87a', b'GIF89a')) else None)
                    if mime:
                        images[member.filename] = f'data:{mime};base64,' + base64.b64encode(data).decode('ascii')
            return html, images
    except (BadZipFile, UnicodeDecodeError, KeyError) as error:
        raise RenderError('Google returned an unreadable HTML export.') from error


def field_definition(raw, index, context):
    legacy = raw.startswith('<')
    if legacy:
        field_type = raw[1:-1].lower()
        key = f'legacy_{field_type}_{index}'
        parts = []
        label = context.strip().strip(':').strip() or f'{field_type.title()} {index}'
        label = label[-120:]
    else:
        pieces = [part.strip() for part in raw[2:-2].split('|')]
        key = pieces.pop(0)
        if not KEY.fullmatch(key) or key.startswith('legacy_'):
            raise RenderError(f'Invalid field name in {raw}. Use names such as client.name.')
        field_type = pieces.pop(0).lower() if pieces and '=' not in pieces[0] and pieces[0] not in {'optional', 'required'} else 'text'
        parts = pieces
        label = key.replace('.', ' ').replace('_', ' ').capitalize()
    if field_type not in TYPES:
        raise RenderError(f'Unknown field type in {raw}. Supported: {", ".join(sorted(TYPES))}.')
    field = {'key': key, 'type': field_type, 'label': label, 'required': True,
             'role': 'vendor' if key.startswith('vendor.') else 'client', 'options': [], 'legacy': legacy}
    seen = set()
    for part in parts:
        name, _, value = part.partition('=')
        if name in seen or (name in {'optional', 'required'} and seen & {'optional', 'required'}):
            raise RenderError(f'Duplicate field setting in {raw}.')
        seen.add(name)
        if part in {'optional', 'required'}:
            field['required'] = part == 'required'
        elif part == 'label':
            raise RenderError(f'Invalid label setting in {raw}. Use label=Your label (an equals sign, not a pipe).')
        elif name == 'label' and 0 < len(value) <= 150:
            field['label'] = value
        elif name == 'default' and '=' in part and value:
            field['default'] = value
        elif name == 'role' and value in {'client', 'vendor'}:
            field['role'] = value
        elif name == 'other' and field_type == 'select' and (part == 'other' or value in {'true', 'false'}):
            field['allow_other'] = part == 'other' or value == 'true'
        elif name == 'options' and field_type == 'select':
            choices = [v.strip() for v in value.split(';')]
            if len(choices) < 2 or len(choices) > 50 or any(not v or len(v) > 100 for v in choices) or len(set(choices)) != len(choices):
                raise RenderError(f'Dropdown {key} needs 2–50 distinct, nonempty options.')
            field['options'] = choices
        else:
            raise RenderError(f'Unknown or invalid setting "{part}" in {raw}.')
    if field_type == 'select' and not field['options']:
        raise RenderError(f'Dropdown {key} needs options=First choice;Second choice.')
    if 'default' in field:
        if field_type in {'signature', 'initials'} or is_signing_date(field):
            raise RenderError(f'Field {key} cannot have a default: signatures, initials, and automatic signing dates must be completed when signing.')
        if field_type == 'checkbox':
            if field['default'] not in {'true', 'false'}:
                raise RenderError(f'Default for checkbox {key} must be true or false.')
            field['default'] = field['default'] == 'true'
        # A prefill may start unchecked; required validation still applies on submission.
        errors = validate_values([dict(field, role='client', required=False)], {key:field['default']})
        if errors:
            raise RenderError(f'Invalid default for {key}: {errors[key]}')
    return field


def render_html(html, images=None):
    soup = BeautifulSoup(html, 'html.parser')
    warnings = []
    # Google currently exports inline formatting; also support simple class rules.
    styles = {}
    for element in soup.find_all('style'):
        for rule in tinycss2.parse_stylesheet(element.get_text(), skip_whitespace=True, skip_comments=True):
            if rule.type == 'qualified-rule':
                selector = tinycss2.serialize(rule.prelude).strip()
                if re.fullmatch(r'\.[A-Za-z_][\w-]*', selector):
                    styles[selector[1:]] = clean_style(tinycss2.serialize(rule.content))
    root = soup.body or soup
    for element in list(root.find_all(['script', 'style', 'iframe', 'object', 'embed', 'svg', 'math', 'form', 'input', 'button', 'textarea', 'select', 'link', 'meta'])):
        element.decompose()
    for comment in root.find_all(string=lambda value: isinstance(value, Comment)):
        comment.extract()
    for element in list(root.find_all(True)):
        if element.name not in SAFE_TAGS:
            element.unwrap()
            continue
        attrs = {}
        css = ';'.join(styles.get(c, '') for c in element.get('class', [])) + ';' + element.get('style', '')
        css = clean_style(css)
        if css:
            attrs['style'] = css
        for span in ('colspan', 'rowspan'):
            if element.name in {'td', 'th'} and str(element.get(span, '')).isdigit():
                attrs[span] = str(min(int(element[span]), 100))
        if element.name == 'img':
            source = (images or {}).get(element.get('src', ''))
            if not source:
                element.replace_with(NavigableString('[Image unavailable]'))
                warnings.append('An image could not be embedded in this snapshot.')
                continue
            attrs.update(src=source, alt=str(element.get('alt', ''))[:200])
        element.attrs = attrs

    fields = {}
    field_tokens = []
    count = 0
    # Process leaf paragraphs/cells so tokens can span formatting runs but not paragraphs.
    blocks = [e for e in root.find_all(['p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'li', 'td', 'th', 'div'])
              if not e.find(['p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'li', 'td', 'th', 'div'])]
    if not blocks:
        blocks = [root]
    for block in blocks:
        nodes = list(block.find_all(string=True))
        text = ''.join(str(node) for node in nodes)
        matches = list(TOKEN.finditer(text))
        residue = TOKEN.sub('', text)
        if '{{' in residue or '}}' in residue or re.search(r'<[A-Z_]{2,}(?:\s|$)', residue):
            raise RenderError(f'Incomplete placeholder near: {text[:160]}')
        replacements = []
        for match in matches:
            count += 1
            if count > 300:
                raise RenderError('A document can contain at most 300 field occurrences.')
            previous = text[:match.start()]
            previous = TOKEN.split(previous)[-1]
            field = field_definition(match.group(), count, previous)
            existing = fields.get(field['key'])
            if existing and existing != field:
                raise RenderError(f'Field {field["key"]} is repeated with conflicting settings. Use identical tags or different names.')
            fields[field['key']] = field
            field_tokens.append({'raw': match.group(), 'key': field['key']})
            replacements.append((match.start(), match.end(), field['key']))
        # Map to original text nodes, preserving all non-token formatting and markup.
        offset = 0
        for node in nodes:
            value = str(node)
            start, end = offset, offset + len(value)
            offset = end
            relevant = [r for r in replacements if r[0] < end and r[1] > start]
            if not relevant:
                continue
            cursor = 0
            for left, right, key in relevant:
                a, b = max(left-start, 0), min(right-start, len(value))
                if a > cursor:
                    node.insert_before(NavigableString(value[cursor:a]))
                if start <= left < end:
                    marker = soup.new_tag('span')
                    marker['data-field-key'] = key
                    node.insert_before(marker)
                cursor = b
            if cursor < len(value):
                node.insert_before(NavigableString(value[cursor:]))
            node.extract()
    remaining = root.get_text()
    if TOKEN.search(remaining) or '{{' in remaining or '}}' in remaining:
        raise RenderError('A placeholder crosses a paragraph or unsupported layout boundary. Keep each complete tag inside one paragraph or table cell.')
    if any(f['legacy'] for f in fields.values()):
        warnings.append('Legacy tags are independent fields with automatically generated names. Use named tags for stable labels and shared values.')
    if any(f['type'] in {'signature', 'initials'} for f in fields.values()):
        warnings.append('Signatures and initials are saved when the client submits the agreement. A preparation-only review check does not submit it.')
    if not fields:
        warnings.append('No fillable tags found. Add placeholders in Google Docs, wait for it to save, then render again.')
    warnings.append('This is a flowing web preview; line wrapping and page breaks may differ from Google Docs. Untagged blanks remain plain text.')
    return {'html': ''.join(str(child) for child in root.contents), 'fields': list(fields.values()),
            'field_tokens': field_tokens, 'occurrences': count, 'warnings': list(dict.fromkeys(warnings)), 'schema_version': 1}


def allows_other(field):
    if field['type'] != 'select':
        return False
    if 'allow_other' in field:
        return field['allow_other']
    # Preserve the behavior of existing tags and stored renders without an explicit setting.
    key = field['key'].lower().replace('.', '_')
    label = field.get('label','').lower()
    return field['type']=='select' and (field.get('allow_other') or any(option.lower()=='other' for option in field.get('options',[])) or key.endswith(('event_type','type_of_event','event_category')) or label in {'type of event','event type'})


def validate_values(fields, values):
    errors = {}
    known = {f['key'] for f in fields if f['role'] == 'client'}
    for key in values.keys() - known:
        errors[key] = 'Unknown field or a field reserved for the vendor.'
    for field in fields:
        if field['role'] != 'client':
            continue
        key, kind = field['key'], field['type']
        value = values.get(key, False if kind == 'checkbox' else '')
        if is_signing_date(field):
            continue  # Set and validated by the server at submission, never client-controlled.
        if kind == 'signature':
            if value is None or value == '':
                if field['required']:
                    errors[key] = 'Add your signature.'
            else:
                error = signature_error(value)
                if error:
                    errors[key] = error
            continue
        if kind == 'checkbox':
            if not isinstance(value, bool) or (field['required'] and value is not True):
                errors[key] = 'Please check this box.'
            continue
        if not isinstance(value, str) or len(value) > 5000:
            errors[key] = 'Enter a value of at most 5,000 characters.'
            continue
        value = value.strip()
        if not value:
            if field['required']:
                errors[key] = 'This field is required.'
            continue
        try:
            if kind == 'number' and not Decimal(value).is_finite():
                raise ValueError()
            if kind == 'date':
                if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value): raise ValueError()
                date.fromisoformat(value)
            if kind == 'time':
                if not re.fullmatch(r'\d{2}:\d{2}(?::\d{2})?', value): raise ValueError()
                parsed = time.fromisoformat(value)
                if parsed.minute % 15 or parsed.second or parsed.microsecond: raise ValueError()
            if kind == 'email' and not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', value): raise ValueError()
            if kind == 'phone' and (not re.fullmatch(r'[+\d\s().xX#-]+', value) or not 7 <= sum(c.isdigit() for c in value) <= 20): raise ValueError()
            if kind == 'select':
                custom = allows_other(field) and value.startswith('Other:') and 0 < len(value[6:].strip()) <= 150
                if not custom and (value not in field['options'] or (allows_other(field) and value.lower()=='other')): raise ValueError()
            if kind == 'initials' and len(value) > 12: raise ValueError()
        except (ValueError, InvalidOperation):
            errors[key] = f'Enter a valid {kind} value.'
    return errors


DEMO_HTML = '''<h2>Harp performance agreement</h2>
<p>Client: {{client.name|text|label=Client name}}</p>
<p>Email: {{client.email|email}}</p><p>Phone: &lt;PHONE&gt;</p>
<p>Event: {{event.type|select|options=Wedding;Corporate;Private event}}</p>
<p>Date: {{event.date|date}} at {{event.start|time}}</p>
<p>Attendees: {{event.attendees|number}}</p><p>Notes: {{event.notes|textarea|optional}}</p>
<p>{{client.confirm|checkbox|label=I have reviewed this preview}}</p>
<p>Client signature: {{client.signature|signature}}</p><p>Vendor signature: {{vendor.signature|signature}}</p>'''
