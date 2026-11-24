"""Run uv run python -m digital_signing.setup_gmail to save credentials privately."""
import getpass
import json
import os
import re
from digital_signing.config import Settings


def main():
    settings = Settings()
    settings.prepare()
    address = input('Gmail / Google Workspace email address: ').strip()
    if not re.fullmatch(r'[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+', address):
        raise SystemExit('Enter a valid email address.')
    password = getpass.getpass('Google app password (hidden; not your regular password): ').replace(' ', '')
    if len(password) != 16 or not password.isalnum():
        raise SystemExit('Expected the 16-character Google app password.')
    path = settings.data_dir / 'gmail-credentials.json'
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, 'w') as file:
        json.dump({'email':address, 'app_password':password}, file)
    print(f'Credentials saved privately in {path}. No email was sent.')
    print('Set SIGNING_EMAIL_MODE=gmail in .env and restart the app to enable sending.')


if __name__ == '__main__':
    main()
