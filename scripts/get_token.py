"""
scripts/get_token.py — Pomocný skript pro získání Supabase access_token.

Přihlásí se přes Supabase Auth (email + password) a vypíše access_token,
který lze použít jako Bearer token pro volání API endpointů.

Použití:
    python scripts/get_token.py <email> <password>

Příklad:
    python scripts/get_token.py test@immersive.dev MojeHeslo123
"""

import sys
import os
from pathlib import Path

# Načti .env z kořene projektu
env_path = Path(__file__).resolve().parent.parent / ".env"
if env_path.exists():
    from dotenv import load_dotenv
    load_dotenv(env_path)

from supabase import create_client


def main() -> None:
    if len(sys.argv) != 3:
        print("Použití: python scripts/get_token.py <email> <password>")
        sys.exit(1)

    email = sys.argv[1]
    password = sys.argv[2]

    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_KEY")

    if not url or not key:
        print("Chyba: SUPABASE_URL nebo SUPABASE_KEY není nastaveno v .env")
        sys.exit(1)

    client = create_client(url, key)

    response = client.auth.sign_in_with_password({"email": email, "password": password})

    if not response.session:
        print("Přihlášení selhalo — zkontroluj email a heslo.")
        sys.exit(1)

    print(response.session.access_token)


if __name__ == "__main__":
    main()
