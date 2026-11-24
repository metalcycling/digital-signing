from io import BytesIO

from PIL import Image
import pytest
from reportlab.pdfgen.canvas import Canvas
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics

from digital_signing.pdf_rendering import render_pdf
from digital_signing.rendering import RenderError
from test_contracts import client, HEADERS


def make_pdf(draw):
    output = BytesIO()
    canvas = Canvas(output, pagesize=(612, 792))
    draw(canvas)
    canvas.save()
    return output.getvalue()


def test_generic_layout_preserves_page_sizes_images_and_repeated_fields():
    def draw(canvas):
        image = Image.new('RGB', (30, 20), '#aa2244')
        canvas.drawImage(ImageReader(image), 500, 700, width=60, height=40)
        canvas.setFont('Helvetica', 10)
        canvas.drawString(60, 650, 'Name: {{client.name|text}}')
        canvas.showPage()
        canvas.setPageSize((792, 612))
        canvas.setFont('Helvetica', 12)
        canvas.drawString(85, 500, 'Confirm: {{client.name|text}}')
    snapshot, assets = render_pdf(make_pdf(draw), '<p>Name: {{client.name|text}}</p><p>Confirm: {{client.name|text}}</p>')
    assert snapshot['layout'] == 'pdf-pages'
    assert [(p['width'], p['height']) for p in snapshot['pages']] == [(612,792), (792,612)]
    assert len(snapshot['fields']) == 1 and snapshot['occurrences'] == 2
    assert [p['placements'][0]['key'] for p in snapshot['pages']] == ['client.name', 'client.name']
    with Image.open(BytesIO(assets[0])) as first:
        assert first.size == (1224, 1584)
        # The image remains where the PDF author put it, without a template-specific rule.
        assert first.getpixel((1020, 144)) == (170, 34, 68)
    with Image.open(BytesIO(assets[1])) as second:
        assert second.size == (1584, 1224)


def test_field_expansion_stops_before_text_or_table_boundary():
    def draw(canvas):
        canvas.setFont('Helvetica', 10)
        canvas.drawString(60, 650, '<TEXT>')
        canvas.drawString(135, 650, 'Protected neighboring text')
        canvas.drawString(300, 650, '<DATE>')
        canvas.line(360, 645, 360, 665)
    snapshot, _ = render_pdf(make_pdf(draw), '<p>&lt;TEXT&gt; Protected neighboring text</p><p>&lt;DATE&gt;</p>')
    a, b = snapshot['pages'][0]['placements']
    assert a['box'][0] + a['box'][2] <= 133.1
    assert b['box'][0] + b['box'][2] <= 358.1


def test_wrapped_tag_has_one_control_and_two_masks():
    def draw(canvas):
        canvas.setFont('Helvetica', 11)
        canvas.drawString(60, 650, '{{event.type|select|')
        canvas.drawString(60, 634, 'options=Wedding;Corporate}}')
    snapshot, _ = render_pdf(make_pdf(draw), '<p>{{event.type|select|options=Wedding;Corporate}}</p>')
    placement = snapshot['pages'][0]['placements'][0]
    assert len(placement['masks']) == 2
    assert snapshot['fields'][0]['options'] == ['Wedding','Corporate']
    assert placement['box'][3] < 15


def test_controls_fit_answers_instead_of_tag_metadata_and_masks_still_cover_tags():
    tags = [
        '{{client.short|text|default=Jane}}',
        '{{client.much_longer_field_name|text|label=A much longer label|default=Jane}}',
        '{{event.count|number|default=1}}',
        '{{event.type|select|options=Wedding;Corporate;Birthday Party|other=false|default=Wedding}}',
        '{{client.checked|checkbox|label=I have read and agree to the terms}}',
    ]
    def draw(canvas):
        canvas.setFont('Helvetica', 10)
        for index, tag in enumerate(tags):
            canvas.drawString(40, 700-index*35, tag)
    snapshot, _ = render_pdf(make_pdf(draw), ''.join(f'<p>{tag}</p>' for tag in tags))
    short, long, number, select, checkbox = snapshot['pages'][0]['placements']
    assert short['box'][2] == long['box'][2]
    assert 40 <= number['box'][2] < 60
    assert select['box'][2] >= pdfmetrics.stringWidth('Birthday Party', 'Helvetica', 10) + 15
    assert select['box'][2] < 110
    assert checkbox['box'][2] < 15
    for placement in (long, select, checkbox):
        assert placement['masks'][0]['box'][2] > placement['box'][2]


def test_long_defaults_get_room_but_remain_bounded_by_nearby_content():
    address = '1641 Example Avenue, Plantation, FL 33317'
    tag = '{{client.address|textarea|default=' + address + '}}'
    # Put a long answer in a short tag position to exercise expansion constraints.
    def draw(canvas):
        canvas.setFont('Helvetica', 10)
        canvas.drawString(40, 700, tag)
        canvas.drawString(40, 650, '<TEXT>')
        canvas.drawString(110, 650, 'Neighbor stays visible')
        canvas.drawString(300, 650, '<TEXT>')
        canvas.drawImage(ImageReader(Image.new('RGB', (10, 10), '#aa2244')), 360, 645, 20, 20)
    snapshot, _ = render_pdf(make_pdf(draw), f'<p>{tag}</p><p>&lt;TEXT&gt;</p><p>&lt;TEXT&gt;</p>')
    address_field, neighbor, image = snapshot['pages'][0]['placements']
    assert address_field['box'][2] >= pdfmetrics.stringWidth(address, 'Helvetica', 10) + 8
    assert address_field['box'][2] < address_field['masks'][0]['box'][2]
    assert neighbor['box'][0] + neighbor['box'][2] <= 108.1
    assert image['box'][0] + image['box'][2] <= 358.1


def test_mismatched_export_blocks_snapshot_instead_of_guessing():
    data = make_pdf(lambda c: c.drawString(60, 650, 'No placeholder here'))
    with pytest.raises(RenderError, match='Could not reliably place'):
        render_pdf(data, '<p>{{client.name|text}}</p>')


@pytest.mark.parametrize('default', ['', '|default=Short', '|default=REQUIRED'])
def test_descriptive_client_fields_have_room_with_short_or_empty_answers(default):
    keys = ['provided_name', 'address', 'venue', 'venue_address', 'pay_by', 'service_requested']
    tags = [f'{{{{client.{key}|text{default}}}}}' for key in keys]
    def draw(canvas):
        canvas.setFont('Helvetica', 11)
        for index, tag in enumerate(tags):
            canvas.drawString(40, 700-index*30, tag)
    snapshot, _ = render_pdf(make_pdf(draw), ''.join(f'<p>{tag}</p>' for tag in tags))
    widths = [p['box'][2] for p in snapshot['pages'][0]['placements']]
    assert widths == [180, 300, 200, 300, 220, 360]


def test_wider_client_fields_still_stop_at_neighboring_text_and_table_borders():
    tags = ['{{client.provided_name|text}}', '{{client.service_requested|text}}']
    def draw(canvas):
        canvas.setFont('Helvetica', 10)
        canvas.drawString(40, 700, tags[0])
        canvas.drawString(180, 700, 'Neighbor')
        canvas.drawString(40, 650, tags[1])
        canvas.line(230, 645, 230, 665)
    snapshot, _ = render_pdf(make_pdf(draw), ''.join(f'<p>{tag}</p>' for tag in tags))
    name, service = snapshot['pages'][0]['placements']
    assert name['box'][0] + name['box'][2] <= 178.1
    assert service['box'][0] + service['box'][2] <= 228.1


def test_repeating_named_header_can_appear_on_each_page():
    def draw(canvas):
        for _ in range(2):
            canvas.drawString(60, 740, '{{client.name|text}}')
            canvas.showPage()
    snapshot, _ = render_pdf(make_pdf(draw), '<p>{{client.name|text}}</p>')
    assert snapshot['occurrences'] == 2
    assert len(snapshot['fields']) == 1


def test_page_assets_persist_and_cannot_be_read_under_another_contract(tmp_path):
    app = client(tmp_path)
    contract = app.post('/api/contracts', json={'name':'Page preview', 'template_id':'demo-harp'}, headers=HEADERS).json()
    base = f'/api/contracts/{contract["id"]}/renders'
    response = app.post(base, headers=HEADERS)
    assert response.status_code == 201
    snapshot = app.get(base + '/1').json()
    assert snapshot['schema_version'] == 2
    page = app.get(snapshot['pages'][0]['image_url'])
    assert page.status_code == 200 and page.content.startswith(b'\x89PNG')
    source = app.get(base + '/1/source.pdf')
    assert source.status_code == 200 and source.content.startswith(b'%PDF')
    assert app.get(base + '/1/pages/99.png').status_code == 404
    assert app.get('/api/contracts/missing/renders/1/pages/1.png').status_code == 404
    restored = client(tmp_path)
    assert restored.get(snapshot['pages'][0]['image_url']).content == page.content
    assert app.post(base, headers=HEADERS).json()['version'] == 2
    assert app.get(base + '/1').json() == snapshot
