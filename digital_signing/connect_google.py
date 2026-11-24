"""One-time local authorization: uv run python -m digital_signing.connect_google FILE"""
import os
import sys
from google_auth_oauthlib.flow import InstalledAppFlow
from digital_signing.config import Settings

SCOPES = ["https://www.googleapis.com/auth/drive"]


def main():
    if len(sys.argv) != 2:
        raise SystemExit("Usage: uv run python -m digital_signing.connect_google /path/to/client_secret.json")
    settings = Settings()
    settings.prepare()
    flow = InstalledAppFlow.from_client_secrets_file(sys.argv[1], SCOPES)
    credentials = flow.run_local_server(port=0, access_type="offline", prompt="consent")
    path = settings.data_dir / "google-token.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as output:
        output.write(credentials.to_json())
    print("Google connected. Set SIGNING_MODE=google and the destination folder in .env.")


if __name__ == "__main__":
    main()
