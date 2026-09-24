"""
db/supabase.py — Inicializace a konfigurace Supabase klienta.

Vytváří singleton instanci Supabase klienta pomocí SUPABASE_URL a
SUPABASE_KEY z prostředí. Ostatní moduly v db balíčku tento klient
importují místo přímé inicializace.

Dle GDD: centralizovaná správa DB připojení zabraňuje resource leakům.
"""

import os
from supabase import create_client, Client

_client: Client | None = None


def get_client() -> Client:
    """Vrací singleton Supabase klienta. Inicializuje při prvním volání."""
    global _client
    if _client is None:
        url = os.environ["SUPABASE_URL"]
        key = os.environ["SUPABASE_KEY"]
        _client = create_client(url, key)
    return _client
