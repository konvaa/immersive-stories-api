# Immersive Stories API

Backend textového fantasy RPG. Pravidla hry vyhodnocuje **deterministický
engine**: akce hráče se klasifikuje, vyhodnotí a změny stavu se aplikují
podle pevných pravidel. **AI (Claude) pak výsledek jen vypráví.** Jazykový
model tedy nerozhoduje o tom, co se ve hře stalo, jen jak to zní. Volitelně
jde scénu vizualizovat přes OpenAI obrazový model za herní kredity.

> **Stav: on hold.** Vývoj je pozastavený a produkční nasazení (Railway)
> neběží. Mobilní klient je v samostatném repozitáři `immersive-stories-app`.

## Stack

- **FastAPI** + Uvicorn (Python 3.10+)
- **Supabase**: Postgres, autentizace (JWT ověřované přes JWKS), přístup přes
  supabase-py a pro transakční `/action` přímo přes asyncpg
- **Anthropic API**: narace
- **OpenAI API**: generování obrázků scén (volitelné)
- pytest, pytest-asyncio, httpx

## Struktura

| Složka | Obsah |
|---|---|
| `engine/` | deterministická pravidla: klasifikace akce, resolver, přechody stavu, ruleset |
| `llm/` | sestavení kontextu a promptů, narátor, prompty pro obrázky |
| `api/routes/` | HTTP endpointy (kampaň, akce, narace, vizualizace, kredity) |
| `db/` | Supabase klient, asyncpg pool, čtení a zápis stavu |
| `models/` | Pydantic modely API a herního stavu |
| `00x_*.sql` | databázové migrace, spouští se ručně v Supabase SQL editoru v pořadí |

## Proměnné prostředí

Vzor s popisem všech proměnných je v [`.env.example`](.env.example).
Zkopíruj ho jako `.env` a vyplň. Nutné minimum:

| Proměnná | K čemu |
|---|---|
| `SUPABASE_URL`, `SUPABASE_KEY` | Supabase projekt; klíč musí být **secret** (serverový) |
| `DATABASE_URL` | přímé Postgres připojení (Session pooler) pro transakce |
| `ANTHROPIC_API_KEY` | narace |
| `OPENAI_API_KEY` | jen pro vizualizace |
| `RULESET_VERSION` | verze pravidel, aktuálně `adventurer-0.1.0` |

## Spuštění lokálně

```sh
python -m venv .venv
.venv\Scripts\activate            # Windows; na Linuxu/macOS: source .venv/bin/activate
pip install -r requirements.txt

# jednorázově: v Supabase SQL editoru spustit 001–004_*.sql v tomto pořadí

uvicorn main:app --reload         # http://localhost:8000, dokumentace API na /docs
```

Testy:

```sh
pytest -m unit                    # jen unit testy, bez serveru a sítě
pytest tests/ -v                  # včetně živých testů; potřebuje běžící server a TEST_JWT_TOKEN
```

Token pro testy: `python scripts/get_token.py <email> <heslo>`.

## Nasazení

Postup a povinné pořadí kroků (migrace před deployem) popisuje
[`docs/DEPLOY.md`](docs/DEPLOY.md).
