"""Build immutable completion PDFs from saved page images and validated values."""
import base64
import re
from io import BytesIO
from pathlib import Path

from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.colors import HexColor
from reportlab.lib.utils import ImageReader

from digital_signing.calculations import time_minutes

FONT = 'ContractSans'
pdfmetrics.registerFont(TTFont(FONT, str(Path(__file__).parent / 'static/fonts/NotoSans-Regular.ttf')))


def signature_reader(value):
    return ImageReader(BytesIO(base64.b64decode(value['image'].split(',', 1)[1])))


def put_signature(pdf, value, x, y, width, height):
    image = signature_reader(value)
    iw, ih = image.getSize()
    scale = min(width / iw, height / ih)
    pdf.drawImage(image, x, y + (height-ih*scale)/2, width=iw*scale, height=ih*scale, mask='auto')


def lines(text, width, size=10):
    # Preserve all content, including newlines and long unbroken values.
    for paragraph in str(text).split('\n'):
        if not paragraph:
            yield ''
            continue
        row = ''
        for char in paragraph:
            if row and pdfmetrics.stringWidth(row + char, FONT, size) > width:
                yield row
                row = ''
            row += char
        yield row


def display_answer(field, value):
    if isinstance(value, bool):
        return 'Yes' if value else 'No'
    if field.get('type') in {'date','signing_date'} and isinstance(value, str):
        return re.sub(r'^(\d{4})-(\d{2})-(\d{2})$', r'\2-\3-\1', value)
    if field.get('type') == 'time' and isinstance(value,str) and re.fullmatch(r'\d{2}:\d{2}(?::\d{2})?',value):
        hour, minute = map(int,value.split(':')[:2])
        return f"{hour % 12 or 12}:{minute:02d} {'AM' if hour < 12 else 'PM'}"
    return value


def create_completed_pdf(snapshot, page_images, values, client, vendor=None):
    output = BytesIO()
    pdf = canvas.Canvas(output, pageCompression=1)
    pdf.setTitle(snapshot['name'] + (' - fully executed' if vendor else ' - client submitted'))
    fields = {f['key']: f for f in snapshot['fields']}
    # Refuse silent replacement by unsupported glyphs in newly entered data.
    supported = pdfmetrics.getFont(FONT).face.charToGlyph
    for value in [*values.values(), client['signer_name'], *([vendor['signer_name']] if vendor else [])]:
        if isinstance(value, str) and any(ord(c) not in supported and not c.isspace() for c in value):
            raise ValueError('An answer contains a character unsupported by the PDF font. Replace that character and try again.')
    for page, image in zip(snapshot['pages'], page_images, strict=True):
        width, height = page['width'], page['height']
        pdf.setPageSize((width, height))
        pdf.drawImage(ImageReader(BytesIO(image)), 0, 0, width=width, height=height)
        for placement in page['placements']:
            for mask in placement['masks']:
                x, y, w, h = mask['box']
                pdf.setFillColor(HexColor(mask['color']))
                pdf.rect(x, height-y-h, w, h, stroke=0, fill=1)
            value = values.get(placement['key'])
            if value is None or value == '':
                continue
            x, y, w, h = placement['box']
            pdf.setFillColor(HexColor('#172d39'))
            if isinstance(value, dict):
                put_signature(pdf, value, x, height-y-h, w, h)
                continue
            text = display_answer(fields.get(placement['key'], {}), value)
            size = min(placement['font_size'], h*.8)
            text_width = pdfmetrics.stringWidth(text, FONT, size)
            if '\n' in text or text_width > w:
                # The full response is always printed in the attached answer record.
                text = 'See answer record'
                size = min(size, w / max(pdfmetrics.stringWidth(text, FONT, 1), 1))
            pdf.setFont(FONT, size)
            pdf.drawString(x, height-y-h+(h-size)/2+size*.18, text)
        pdf.showPage()

    y = 0
    record_page = 0
    def new_record_page():
        nonlocal y, record_page
        record_page += 1
        pdf.setPageSize((612, 792))
        pdf.setFillColor(HexColor('#172d39')); pdf.setFont(FONT, 16)
        pdf.drawString(40, 752, 'Agreement completion record' if vendor else 'Client submission record')
        pdf.setFont(FONT, 9); pdf.drawString(40, 732, f"{snapshot['contract_id']} / version {snapshot['version']}")
        pdf.setFont(FONT, 8); pdf.drawRightString(572, 24, f'Completion record {record_page}')
        y = 707
    def write(text, size=10):
        nonlocal y
        for line in lines(text, 532, size):
            if y < 45:
                pdf.showPage(); new_record_page()
            pdf.setFont(FONT, size); pdf.drawString(40, y, line)
            y -= size*1.5
    new_record_page()
    write(snapshot['name'], 12)
    write('Status: Fully executed' if vendor else 'Status: Awaiting vendor signature')
    write('Source PDF SHA-256: ' + snapshot['source_sha256'], 8)
    for label, record in [('Client', client), ('Vendor', vendor)]:
        if not record:
            continue
        write('')
        write(f"{label}: {record['signer_name']}", 12)
        write(f"Recorded at: {record['signed_at']} (UTC)", 9)
        write('Consent: ' + record['consent_text'], 9)
    write('')
    write('Answer record', 13)
    ordered_keys = [f['key'] for f in snapshot['fields'] if f['key'] in values]
    ordered_keys.extend(key for key in values if key not in fields)
    for key in ordered_keys:
        value = values[key]
        if y < (155 if isinstance(value, dict) else 90):
            pdf.showPage(); new_record_page()
        field = fields.get(key, {'label': 'Vendor signature', 'role': 'vendor'})
        write(f"{field['label']} ({field['role']})", 10)
        if isinstance(value, dict):
            if y < 115:
                pdf.showPage(); new_record_page()
            put_signature(pdf, value, 40, y-60, 300, 60)
            y -= 75
            write('Signature method: ' + value['method'], 8)
        else:
            write(display_answer(field, value) or '(blank)', 10)
            calc = field.get('calc', {})
            if calc.get('function') == 'duration_hours':
                start, end = [time_minutes(values.get(key)) for key in calc['sources']]
                if start is not None and end is not None and end < start:
                    write('End time is on the following day.', 10)
        y -= 6
    pdf.save()
    return output.getvalue()
