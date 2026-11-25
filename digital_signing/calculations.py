"""Small, explicit calculated fields. Expressions never execute arbitrary code."""
import re
from datetime import date
from decimal import Decimal

NAME = r'[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*'
WEEKDAYS = ('Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday')


def parse_calculation(expression):
    match = re.fullmatch(r'(weekday|duration_hours)\(([^()]*)\)', expression.strip())
    if not match:
        raise ValueError('Use calc=weekday(date.field) or calc=duration_hours(start.field,end.field).')
    function, args = match.groups()
    sources = [value.strip() for value in args.split(',')]
    count = 1 if function == 'weekday' else 2
    if len(sources) != count or any(not re.fullmatch(NAME, key) for key in sources):
        raise ValueError(f'{function} needs {count} valid source field name(s).')
    return {'function': function, 'sources': sources}


def validate_calculations(fields):
    by_key = {field['key']: field for field in fields}
    for field in fields:
        calc = field.get('calc')
        if not calc:
            continue
        expected = 'date' if calc['function'] == 'weekday' else 'time'
        if len(set(calc['sources'])) != len(calc['sources']):
            raise ValueError(f"Calculation {field['key']} needs different start and end fields.")
        for key in calc['sources']:
            source = by_key.get(key)
            if not source:
                raise ValueError(f"Calculation {field['key']} refers to missing field {key}.")
            from digital_signing.signing_rules import is_signing_date
            if key == field['key'] or source.get('calc') or source['type'] != expected or is_signing_date(source):
                raise ValueError(f"Calculation {field['key']} needs an ordinary {expected} source: {key}.")
            if source['role'] != field['role']:
                raise ValueError(f"Calculation {field['key']} and source {key} must belong to the same signer.")


def time_minutes(value):
    if not isinstance(value, str) or not re.fullmatch(r'(?:[01][0-9]|2[0-3]):(?:00|15|30|45)(?::00)?', value.strip()):
        return None
    hour, minute = value.strip().split(':')[:2]
    return int(hour)*60 + int(minute)


def calculate_values(fields, values, role='client'):
    """Replace submitted results with authoritative calculations, without mutating inputs."""
    result, errors = dict(values), {}
    for field in fields:
        calc = field.get('calc')
        if not calc or field['role'] != role:
            continue
        key, sources = field['key'], calc['sources']
        result[key] = ''
        inputs = [values.get(source, '') for source in sources]
        missing = [source for source, value in zip(sources, inputs) if value is None or (isinstance(value, str) and not value.strip())]
        if missing:
            if field['required']:
                for source in missing:
                    errors[source] = f"Complete this field to calculate {field['label']}."
            continue
        if calc['function'] == 'weekday':
            value = inputs[0]
            try:
                if not isinstance(value, str) or not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', value.strip()):
                    raise ValueError()
                result[key] = WEEKDAYS[date.fromisoformat(value.strip()).weekday()]
            except ValueError:
                errors[sources[0]] = 'Enter a valid date to calculate the day of the week.'
        else:
            minutes = [time_minutes(value) for value in inputs]
            invalid = [source for source, minute in zip(sources, minutes) if minute is None]
            if invalid:
                for source in invalid:
                    errors[source] = 'Choose a valid time in 15-minute intervals to calculate the duration.'
            elif minutes[0] == minutes[1]:
                errors[sources[1]] = 'Choose different start and end times; the event duration must be greater than zero.'
            else:
                duration = Decimal((minutes[1]-minutes[0]) % 1440) / 60
                result[key] = format(duration, 'f')
    return result, errors
