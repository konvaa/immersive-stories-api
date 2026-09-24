# Ruleset reconciliation: GDD v0.8/v0.9 vs. kód

Stav k 2026-06-13. Účel: sladit, co GDD specifikuje, s tím, co engine reálně dělá,
před stavbou prvního reálného rulesetu + obtížnosti. Zdroje: GDD v0.8 Resolution
Addendum (4.1–4.6), v0.8 sekce 2.6/2.7, Addendum B (15.6), Addendum C; kód
`engine/resolver.py`, `engine/effect_applier.py`, `models/state.py`, `models/api.py`,
`001_initial_schema.sql`.

## A. Kde GDD a kód SEDÍ
- Základ d20 + DC: kód hází d20 deterministicky ze seedu (GDD 20.4 — `ruleset_version`
  JE součástí seedu, ověřeno v `build_rng_seed`).
- player_state: kód `PlayerSnapshot` má `hp`, `max_hp`, `gold`, `flags`, `knowledge`,
  `location_id` — odpovídá Addendum B 15.6. `context_builder` HP/gold surfacuje narátorovi.
- `stakes` pole existuje v DB (`events.stakes`) i ve writeru.

## B. Kde se ROZCHÁZEJÍ (kód zaostává za GDD)
| Téma | GDD | Kód dnes |
|------|-----|----------|
| DC | `base_dc` per ActionDefinition (Drow příklad 0–25) | fixní `dc = 10` pro vše |
| Modifikátory hodu | skill bonus +0..+4 (level 1–24→0, 25–49→1, 50–74→2, 75–99→3, 100→4); knowledge +1..+3; mentor +1..+3; equipment +1..+2; fatigue/injury −1..−3; hostile env −1..−3 | žádné — čistý d20 |
| Outcome tiery | 6 úrovní s „cost" (4.4) | 4 (CRIT≥18 / SUCCESS≥DC / PARTIAL≥DC−3 / FAILURE) |
| Natural 1 / 20 | crit fail / exceptional bez ohledu na mods | NEimplementováno |
| Stakes | LOW/MEDIUM/HIGH/EXTREME per akce | default `"NORMAL"`, resolver nepočítá |
| Efekt škálování | Interaction Pressure (2.7): base × resistance × diminishing × context | fixní delty (trust +10/+20, atd.) |
| `ActionResponse.skills` | zmíněno | chybí |
| Combat na hráče | „consequence ceiling" dle stakes | ATTACK jen na NPC, hráč nemůže utrpět/zemřít |
| ActionDefinition schema | `{action_type, base_dc, base_stakes}` | neexistuje |

## C. Co GDD nechává OTEVŘENÉ → design decision
1. Názvy + rozsah hráčských statů/skillů (GDD je nedefinuje jménem).
2. Mapování `action_type` → skill/stat.
3. Konkrétní formát ruleset config souboru (GDD říká „ruleset files", bez struktury).
4. Combat damage čísla; zda a jak může hráč „prohrát".
5. Jak přesně `world_pressure` modifikuje DC (GDD: implicitní v „environment_modifiers").

## D. K OVĚŘENÍ z GDD (do web vlákna)
1. ✅ **VYŘEŠENO (2026-06-13):** outcome je keyovaný na **margin = (d20 + mods) − DC**.
   Viz finalizovaný systém níže (sekce F).
2. ✅ **VYŘEŠENO:** DC vstupuje přes margin; prahy jsou relativní k DC a jsou
   součástí difficulty configu (easy/medium/hard posouvají prahy).
3. Sekce 2.7 — konkrétní vzorce/čísla pro resistance / diminishing returns / context, pokud jsou. (OTEVŘENÉ)
4. Existuje ActionDefinition tabulka pro **Adventurer** template (base_dc/stakes per akce),
   nebo jen Drow příklad? Pokud jen Drow → musíme nadefinovat Adventurer. (OTEVŘENÉ — zatím placeholdery)

## F. FINALIZOVANÝ resolution systém (implementováno v `engine/ruleset.py`)
```
total  = d20 + součet modifikátorů (skill +0..+4, knowledge, mentor, equipment, fatigue, environment)
margin = total - DC
```
- **Natural 1** → vždy `CATASTROPHIC_FAILURE` (severitu stropuje Stakes).
- **Natural 20** → margin ≥ 0: `EXCEPTIONAL_SUCCESS`; margin < 0: `FAILURE` + `mitigated=True`
  (nejmírnější selhání, žádný vážný následek — mág kouzlo nezvládne, ale přežije).
- **Roll 2–19** → tier dle margin prahů (default "medium"):

| margin | tier |
|--------|------|
| ≥ +7 | CLEAN_SUCCESS_BONUS |
| +2..+6 | CLEAN_SUCCESS |
| 0..+1 | SMALL_SUCCESS |
| −1..−2 | FAILURE |
| −3..−5 | SEVERE_FAILURE |
| ≤ −6 | HARD_FAILURE |

Stakes (LOW/MEDIUM/HIGH/EXTREME) určují narativní strop „katastrofy"/„serious cost",
neovlivňují margin ani DC. Default prahy ověřeny: přesně reprodukují příklad DC 12
(total 1–6 hard / 7–9 severe / 10–11 fail / 12–13 small / 14–18 clean / 19+ bonus).

### VYŘEŠENO 2026-06-13 (zapracováno v engine/ruleset.py)
- **C1 core skilly:** `awareness`, `resolve`, `presence`, `finesse` (0–100). Template
  přidává vlastní (Drow: arcana/stealth/deception). `resolve` zatím bez akce —
  rezerva pro odolnostní checky.
- **C2 action→skill** (`ACTION_SKILL_MAP`): OBSERVE/INVESTIGATE→awareness,
  INFLUENCE/MANIPULATE→presence, ATTACK/MOVE/INTERACT→finesse, WAIT→žádný.
- **D4 Adventurer base_dc** (Crossroads Inn) zapsáno do `ADVENTURER_RULESET`;
  jemné intenzity (careful=14, casual=4) v `ActionDefinition.variants`.

### VYŘEŠENO 2026-06-13 (druhá dávka, zapracováno v engine/ruleset.py)
- **MANIPULATE** = samostatný typ, GDD „change known facts / hidden state"
  (přepis dokumentu, falšování stopy, skrytí důkazů) — NE osoby/objekt. Nízké base_dc=8,
  skill `presence`, `affected_by_pressure=True`. Pozn.: D4 „Pressure/threaten NPC DC15"
  patří pod INFLUENCE (sociální nátlak na osobu) → přesunuto do `INFLUENCE.variants["pressure"]`.
- **STEALTH** = nový samostatný ActionType, skill `finesse`, base_dc=12 MEDIUM.
- **world_pressure → roll penalty** přes `world_pressure_modifier()`: 0–2→0, 3–4→−1,
  5–6→−2, 7–9→−3, 10+→−4. Aplikuje se POUZE na sociální akce (`affected_by_pressure=True`:
  INFLUENCE, MANIPULATE) — při napětí jsou NPC ostražitější. NEovlivňuje fyzické akce
  (INVESTIGATE/MOVE/STEALTH/INTERACT/ATTACK); fyzické rozdíly prostředí (dřevo vs ocel)
  řeší base_dc, ne world_pressure.

### VYŘEŠENO 2026-06-13 (třetí dávka — combat/HP, v engine/ruleset.py)
- **4 difficulty tiery** (`DifficultyProfile.death_allowed`): `story` (smrt ne, následky
  narativní), `normal` (smrt ne, vážné následky — ověřené prahy = DC 12 příklad),
  `hard` (smrt ano), `insane` (smrt ano, přísnější prahy). Registr `DIFFICULTY_PROFILES`.
- **HP** je template-specific. Adventurer: `ADVENTURER_MAX_HP=20`. 0 HP → `resolve_hp_zero()`:
  story/normal → `knocked_out` (HP reset na 1 + narativ), hard/insane → `game_over`
  (Flutter „You have fallen", restart/load).
- **Combat (Adventurer, příklad):** `DiceSpec` (+parser), `ADVENTURER_WEAPONS`
  (unarmed 1d4 / dagger 1d6 / sword 1d8 / greatweapon 1d10+2). `melee_damage()` =
  zbraň + finesse bonus + 1d4 při marginu ≥7, − armor. Hodnoty zbraní = příklad pro
  mini scénu, full gear systém přijde s full templatem.
- **NPC combat** (`NPCCombatProfile`): `attack_dc`, `damage_dice`, `can_initiate`
  (NPC smí zaútočit samo — loupež/přepad/monstrum, jako world event, ne jen reakce).

### ✅ VYŘEŠENO 2026-07-04 — zapojení do živého resolveru (sekce E)
Jeden čistý zásah, kompletní (testy: `tests/test_resolver_ruleset.py`, 35 unit testů):
- `engine/classifier.py`: ActionType `MANIPULATE` + `STEALTH` + regex patterny
  (MANIPULATE má prioritu před STEALTH: „hide the evidence" ≠ „hide behind the bar").
- resolver: `dc = get_ruleset(world.ruleset_version).action(type).base_dc`;
  `modifiers = skill bonus + world_pressure (jen affected_by_pressure) + situační
  znalost (+1: INVESTIGATE po observed_loc, MANIPULATE po investigated_loc)`;
  outcome přes `resolve_outcome()` (8 tierů, natural 1/20, margin prahy).
- Efekty STEALTH (hidden flag / suspicion) a MANIPULATE (manipulated_ knowledge +
  location flag "tampered" / suspicion); INFLUENCE delty škálované tierem.
- Combat: ATTACK → `melee_damage()` (zbraň z player flags `weapon:*`, default unarmed);
  selhání → protiútok NPC (`npc_combat_profile`, roll ≥ attack_dc); NPC iniciativa
  (`can_initiate` + suspicion ≥ 80, max 1 za tick) jako world event; 0 HP →
  `resolve_hp_zero()` → SET hp=1 + flag `knocked_out` (story/normal) / flag `game_over`
  (hard/insane).
- `ActionResponse`: 8-tier outcome + `stakes` + `mitigated`; `events.mitigated` sloupec.
- Difficulty: `campaigns.difficulty` (create API validuje) → `DIFFICULTY_PROFILES[tier]`.
- Migrace `004_adventurer_ruleset.sql`: difficulty + mitigated sloupce,
  ruleset_version → adventurer-0.1.0, player skilly + HP škála 20 (i NPC 100→20).
- Deterministický kontrakt: PRVNÍ `randint(1,20)` z rng_seed = hráčův d20 (AT-10
  replay); damage/protiútok/iniciativa čerpají ze stejného rng ve fixním pořadí.

Design rozhodnutí učiněná při zásahu (k případné revizi):
- NPC iniciativa: prah suspicion ≥ 80 (`NPC_INITIATIVE_SUSPICION`); Sheriff
  can_initiate=True (attack_dc 10, 1d6), Innkeeper ne (14, 1d4).
- HP škála: hráč i NPC na 20 (Innkeeper 16, Sheriff 20) — čitelné vs. melee 1d4–1d10+2.
- Mitigated selhání = poloviční suspicion dopad, žádná ztráta trustu, žádný protiútok.

### ✅ Stabilizační patch 2026-07-10 (audit nálezy K2/K3/K4/K5-min/V3/K1)
- **K2:** idempotency lookup scoped na `(campaign_id, request_id)` — konec
  cross-campaign cache hitů (`tests/test_db_contracts.py`, AT-2b).
- **K4:** NPC + lokace se načítají `ORDER BY created_at, id` — cíl ATTACKu
  a pořadí iniciativy jsou deterministické.
- **K3:** UNKNOWN akce nespouští NPC iniciativu — mechanicky bezpečná (AT-12c).
- **K5-min:** /action vrací 409 při `game_over` flagu (až PO idempotency
  checku — retry fatální akce vrací cache); NPC s hp≤0 neiniciuje, není cíl
  combat/social efektů (`_living_npcs_in_location`). Flutter UI odloženo.
- **V3:** MANIPULATE matchuje jen jednoznačná slovesa (forge/falsify/tamper)
  a slovesné fráze s členem („plant the dagger" ano, „look at the plant" ne).
- **K1:** `docs/DEPLOY.md` (povinné pořadí: migrace 004 PŘED deployem),
  `scripts/dev_reset_campaigns.sql` pro wipe dev dat.

Vědomě odloženo z auditu: V1 balancing sociální smyčky (EV INFLUENCE, suspicion
decay, práh iniciativy), world_pressure růst (V4), validace ruleset_override (V5),
mitigated mrtvý kód (V2), event sourcing pro NPC iniciativu (S2), Flutter
game-over obrazovka.

### Stále OTEVŘENÉ (další krok)
- DISCOVER_EXIT při CLEAN_SUCCESS_BONUS+ na INVESTIGATE — čeká na podporu
  known_exits update v `effect_applier` + writeru (TODO v resolveru).
- Target parsing pro ATTACK (nyní první NPC v lokaci) — ActionAttempt vrstva (Addendum I).
- Sekce 2.7 vzorce resistance/diminishing returns (D3) — dosud jen tier škálování delt.

## E. Navržené pořadí stavby (po vyjasnění D)
1. Datový model: `ActionDefinition` (action_type, base_dc, base_stakes) + ruleset config soubor.
2. Player staty/skilly do `player_state` (po rozhodnutí C1/C2).
3. Resolver: `roll + modifiers vs base_dc`, 6-tier outcome + natural 1/20, výpočet stakes.
4. Rozšířit `ActionResponse` outcome enum (4→6) + případně `skills`.
5. Efekt škálování dle marginu/stakes (zjednodušená Interaction Pressure).
6. Combat: definovat damage + player HP důsledky.
7. Migrace `ruleset_version` + obsah (Adventurer ActionDefinitions, encounter data lokací).
