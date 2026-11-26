import httpx
import pytest

from digital_signing.addresses import validate_address
from digital_signing.rendering import render_html, validate_values


@pytest.mark.parametrize('confirmation,valid', [('Y', True), ('D', True), ('N', False), ('', False)])
def test_submission_checks_building_and_excludes_unit(monkeypatch, confirmation, valid):
    def post(url, *, headers, json, timeout):
        assert json['address'] == {'addressLines':['123 Main St'], 'locality':'Boston',
                                   'administrativeArea':'MA', 'postalCode':'02108','regionCode':'US'}
        return httpx.Response(200, request=httpx.Request('POST',url), json={'result':{
            'address':{}, 'uspsData':{'dpvConfirmation':confirmation,
            'standardizedAddress':{'state':'MA','zipCode':'02108'}}}})
    monkeypatch.setattr(httpx, 'post', post)
    fields = render_html('<p>{{client.address|textarea}}</p>')['fields']
    assert fields[0]['type'] == 'address'
    errors = validate_values(fields, {'client.address':'123 Main St, Apt 3, Boston, MA 02108'}, address_api_key='test')
    assert (not errors) == valid


def test_missing_service_does_not_accept_unverified_address():
    assert 'unavailable' in validate_address('123 Main St, Boston, MA 02108','')
    assert 'five-digit' in validate_address('123 Main St, Boston, MA 2108','')
