"""Store a Hetzner project API token privately without printing it."""
import getpass
import os
from pathlib import Path


def main():
    token = getpass.getpass('Hetzner project API token (hidden): ').strip()
    if not token or any(char.isspace() for char in token):
        raise SystemExit('Enter a nonempty token without spaces.')
    directory = Path.home()/'.config'/'bello-signing'
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory/'hetzner-token'
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, 'w') as output:
        output.write(token+'\n')
    print('Hetzner token saved privately. No server has been created.')


if __name__ == '__main__':
    main()
