"""Fixed Google PDF pages with coordinate-based field overlays, independent of template.

All coordinates are PDF points (1/72 inch); the browser scales the whole page.
The PDF raster remains untouched. Small opaque masks hide only placeholder text.
"""
from collections import defaultdict
from html import escape
from io import BytesIO
import re
import threading

from bs4 import BeautifulSoup
import pdfplumber
import pypdfium2 as pdfium
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.pdfbase import pdfmetrics
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer

from digital_signing.rendering import RenderError, render_html

# PDFium is not thread safe. Keep even requests from different app factories serialized.
PDF_LOCK = threading.Lock()

# Room for names, locations, and descriptive answers even with short defaults.
# These field-name conventions apply across templates; collision limits still win.
FIELD_MIN_WIDTHS = {
    'client.provided_name': 180,
    'client.address': 300,
    'client.venue': 200,
    'client.venue_address': 300,
    'client.pay_by': 220,
    'client.service_requested': 360,
}


def demo_pdf(html):
    output = BytesIO()
    styles = getSampleStyleSheet()
    story = []
    for element in BeautifulSoup(html, 'html.parser').find_all(['h2', 'p']):
        story.extend([Paragraph(escape(element.get_text()), styles['Heading2' if element.name == 'h2' else 'BodyText']), Spacer(1, 12)])
    SimpleDocTemplate(output, pagesize=(612, 792), leftMargin=36, rightMargin=36,
                      topMargin=36, bottomMargin=36).build(story)
    return output.getvalue()


def line_rectangles(chars):
    rows = []
    for char in sorted(chars, key=lambda c: (round(c['top'], 1), c['x0'])):
        row = next((row for row in rows if abs(row[0]['top'] - char['top']) <= max(2, char['height'] * .25)), None)
        if row is None:
            rows.append([char])
        else:
            row.append(char)
    return [[min(c['x0'] for c in row), min(c['top'] for c in row),
             max(c['x1'] for c in row), max(c['bottom'] for c in row)] for row in rows]


def background(image, rectangle, scale):
    x0, y0, x1, y1 = rectangle
    crop = image.crop((max(0, int((x0-.5)*scale)), max(0, int((y0-.5)*scale)),
                       min(image.width, int((x1+.5)*scale)+1), min(image.height, int((y1+.5)*scale)+1))).convert('RGB')
    colors = crop.getcolors(crop.width * crop.height) or []
    color = max(colors, default=(0, (255, 255, 255)), key=lambda pair: pair[0])[1]
    return '#%02x%02x%02x' % color


def control_width(field, font_size):
    """Size the answer control independently of the tag text hidden underneath."""
    kind = field['type']
    scale = font_size / 11
    if kind in {'checkbox', 'initials', 'signature'}:
        return {'checkbox': 12, 'initials': 62, 'signature': 155}[kind] * scale
    value = field.get('default', '')
    value = str(value).strip() if value is not None else ''
    if value.upper() == 'REQUIRED':
        value = ''
    if field.get('calc', {}).get('function') == 'weekday':
        value = 'Wednesday'
    elif kind in {'date', 'signing_date'}:
        value = '09-30-2027'
    elif kind == 'time':
        value = '12:45 PM'
    choices = [value]
    if kind == 'select':
        choices += field.get('options', [])
        if field.get('allow_other'):
            choices.append('Other')
    minimum = {'number': 48, 'time': 65, 'phone': 95, 'select': 80}.get(kind, 72) * scale
    minimum = max(minimum, FIELD_MIN_WIDTHS.get(field['key'], 0) * scale)
    if not any(choices):
        return max(minimum, {'text': 140, 'textarea': 180, 'email': 180, 'phone': 105,
                             'number': 54}.get(kind, 140) * scale)
    text_width = max(pdfmetrics.stringWidth(line, 'Helvetica', font_size)
                     for choice in choices for line in (choice.splitlines() or ['']))
    padding = (22 if kind == 'select' else 12) * scale
    return max(minimum, min(text_width + padding, 270 * scale))


def control_box(page, match, rectangles, field):
    first = rectangles[0]
    x0, y0, x1, y1 = first
    # Preserve the placeholder's line height, expanding only into empty horizontal space.
    top, bottom = max(0, y0-.6), min(page.height, y1+.6)
    right = page.width - min(24, page.width * .03)
    own = {id(char) for char in match['chars']}
    obstacles = [c for c in page.chars if id(c) not in own and c['text'].strip()]
    obstacles += page.images
    # A table's vertical rules bound the usable cell just as printed text does.
    obstacles += [edge for edge in page.edges if abs(edge['x1']-edge['x0']) < 1]
    for other in obstacles:
        if other['x0'] >= x1 - .1 and other['top'] < bottom and other['bottom'] > top:
            right = min(right, other['x0'] - 2)
    font_size = max(7, min(12, float(match['chars'][0]['size'])))
    desired = control_width(field, font_size)
    # Full-size masks still hide every tag character, even when the box is shorter.
    width = max(1, min(desired, right-x0, page.width-x0))
    return [round(x0, 3), round(top, 3), round(width, 3), round(bottom-top, 3)]


def render_pdf(pdf_bytes, html, images=None):
    schema = render_html(html, images)
    tokens = schema.pop('field_tokens')
    schema.pop('html')
    schema['schema_version'] = 2
    schema['layout'] = 'pdf-pages'
    schema['warnings'] = [w for w in schema['warnings'] if not w.startswith(('This is a flowing', 'An image could not'))]
    schema['warnings'].append('Pages use the Google PDF layout. Fields expand only into available space; use the field editor for longer answers. Untagged blanks remain fixed text.')
    if len(pdf_bytes) > 30_000_000:
        raise RenderError('The PDF is too large to preview (30 MB limit).')
    groups = defaultdict(list)
    for token in tokens:
        groups[re.sub(r'\s+', '', token['raw'])].append(token)
    fields = {f['key']: f for f in schema['fields']}
    pages = []
    assets = []
    matches_by_token = defaultdict(list)
    with PDF_LOCK, pdfplumber.open(BytesIO(pdf_bytes)) as document, pdfium.PdfDocument(pdf_bytes) as raster:
        if not 1 <= len(document.pages) <= 50:
            raise RenderError('Preview supports documents of 1–50 pages.')
        for page in document.pages:
            if page.width > 2000 or page.height > 2000:
                raise RenderError('This page size is too large to preview.')
            for compact in groups:
                pattern = r'\s*'.join(re.escape(char) for char in compact)
                for match in page.search(pattern, return_chars=True) or []:
                    if any(not c.get('upright', True) for c in match['chars']):
                        raise RenderError('A field tag is rotated. Keep placeholder text horizontal.')
                    matches_by_token[compact].append((page.page_number-1, match))
        assignments = defaultdict(list)
        for compact, source_tokens in groups.items():
            matches = matches_by_token[compact]
            # Repeating named fields in page headers/footers may produce additional PDF occurrences.
            same_key = len({t['key'] for t in source_tokens}) == 1
            if len(matches) != len(source_tokens) and not (same_key and matches):
                raise RenderError(f'Could not reliably place {source_tokens[0]["raw"]}: Google HTML contains {len(source_tokens)} occurrence(s), PDF contains {len(matches)}. Keep tags together and wait for Docs to save before rendering again.')
            for index, (page_index, match) in enumerate(matches):
                token = source_tokens[min(index, len(source_tokens)-1)]
                assignments[page_index].append((match, token['key']))
        for index, page in enumerate(document.pages):
            pdf_page = raster[index]
            scale = 2  # 144 dpi, with CSS geometry still measured in PDF points.
            bitmap = pdf_page.render(scale=scale)
            image = bitmap.to_pil().convert('RGB')
            placements = []
            for match, key in sorted(assignments[index], key=lambda item: (item[0]['top'], item[0]['x0'])):
                rects = line_rectangles(match['chars'])
                placements.append({'key': key, 'box': control_box(page, match, rects, fields[key]),
                    'font_size': max(7, min(12, float(match['chars'][0]['size']))),
                    'masks': [{'box': [r[0]-.25, r[1]-.25, r[2]-r[0]+.5, r[3]-r[1]+.5],
                               'color': background(image, r, scale)} for r in rects]})
            output = BytesIO()
            image.save(output, format='PNG')
            assets.append(output.getvalue())
            text = page.extract_text() or ''
            for compact, source_tokens in groups.items():
                pattern = r'\s*'.join(re.escape(char) for char in compact)
                text = re.sub(pattern, lambda _: f'[{fields[source_tokens[0]["key"]]["label"]}]', text)
            pages.append({'number': index+1, 'width': float(page.width), 'height': float(page.height),
                          'placements': placements, 'text': text})
            image.close()
            bitmap.close()
            pdf_page.close()
    if sum(len(asset) for asset in assets) > 80_000_000:
        raise RenderError('Rendered pages exceed the 80 MB preview limit.')
    schema['pages'] = pages
    schema['occurrences'] = sum(len(p['placements']) for p in pages)
    return schema, assets
