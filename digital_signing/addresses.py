"""US building address checks. Secondary unit details stay in the agreement."""
import re

import httpx

STATES = set("AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY PR".split())
ADDRESS = re.compile(r"(?P<street>[0-9][^,\n]{1,119}), (?:((?:Apt|Suite|Room|Other) [^,\n]{1,40}), )?(?P<city>[^,\n]{1,80}), (?P<state>[A-Z]{2}) (?P<zip>[0-9]{5})")
FORMAT_ERROR = "Enter the street number and street name, city, state, and five-digit ZIP code."


def validate_address(value, api_key):
    match = ADDRESS.fullmatch(value)
    if not match or match['state'] not in STATES:
        return FORMAT_ERROR
    unavailable = "Address verification is unavailable. Please try again later or contact the sender."
    if not api_key:
        return unavailable
    address = dict(addressLines=[match['street']], locality=match['city'],
                   administrativeArea=match['state'], postalCode=match['zip'],
                   regionCode='US')
    try:
        response = httpx.post('https://addressvalidation.googleapis.com/v1:validateAddress',
                              headers={'X-Goog-Api-Key': api_key}, timeout=5,
                              json={'address': address, 'enableUspsCass': True})
        response.raise_for_status()
        result = response.json()['result']
        components = result['address']
        usps = result.get('uspsData', {})
        standardized = usps.get('standardizedAddress', {})
        if (usps.get('dpvConfirmation') in {'Y', 'D', 'S'}
                and standardized.get('state') == match['state']
                and standardized.get('zipCode') == match['zip']
                and not components.get('unresolvedTokens')
                and not (set(components.get('unconfirmedComponentTypes', [])) - {'subpremise'})
                and not any(c.get('replaced') or c.get('spellCorrected') for c in
                            components.get('addressComponents', []) if c.get('componentType') != 'subpremise')):
            return None
    except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError):
        return unavailable
    return "We couldn't confirm this street address, city, state, and ZIP together. Check them and try again."
