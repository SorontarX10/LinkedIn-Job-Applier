# Plan wdrozenia: Copilot Agent do aplikowania na LinkedIn

## 1. Cel i wynik koncowy
Zbudowac w pelni efektywnego agenta copilota, ktory:
1. Aplikuje z zapisanych ofert jak obecnie.
2. Samodzielnie odzyskuje kontrole po zacieciu formularza przez tryb LLM + Playwright tool calls.
3. Uczy sie nowych sytuacji z automatycznych runow i interwencji uzytkownika.
4. Dodatkowo aktywnie wyszukuje oferty po wymaganiach oraz po dopasowaniu do CV/profilu.
5. Priorytetyzuje i kolejkuje najlepsze oferty do aplikowania.

Definicja "done":
1. Agent konczy stabilnie run na 3 testowych ofertach bez terminalowego inputu.
2. W sytuacji zaciecia potrafi przejsc do agentic fallback i wykonac sensowna sekwencje akcji.
3. Po interwencji czlowieka bot automatycznie wznawia i zapisuje wiedze.
4. Tryb wyszukiwania ofert dostarcza liste kandydatow z rankingiem i deduplikacja.

## 2. Zakres funkcjonalny
Zakres obejmuje:
1. Usprawnienie obecnych flow Easy Apply i External Apply.
2. Wdrozenie warstwy narzedziowej dla LLM (tool executor nad Playwright).
3. Wdrozenie uczenia recept akcji i odpowiedzi formularzowych.
4. Wdrozenie modulu "Job Discovery" (wyszukiwanie ofert po kryteriach i CV).
5. Wdrozenie metryk, logow i testow E2E.

Poza zakresem:
1. Integracja z platnymi API LinkedIn.
2. Omijanie zabezpieczen anty-bot niezgodne z ToS.
3. Pelna orkiestracja wielosesyjna/multikonto.

## 3. Architektura docelowa
Architektura oparta o 5 warstw:
1. Deterministic Runner: obecny pipeline na selectorach i heurystykach.
2. Agentic Fallback: LLM sterujacy zestawem bezpiecznych narzedzi Playwright.
3. Memory & Learning: wiedza o polach, receptach akcji i historii wynikow.
4. Job Discovery & Ranking: wyszukiwanie, filtrowanie i scoring ofert.
5. Observability & Safety: metryki, limity krokow, warunki stop i handoff.

Przeplyw:
1. Discovery buduje kolejke ofert.
2. Runner bierze oferte z kolejki.
3. Gdy flow sie zacina, runner odpala Agentic Fallback.
4. Gdy fallback nie moze bezpiecznie isc dalej, uruchamia human handoff.
5. Po runie wyniki i nowe wzorce sa zapisywane do knowledge store.

## 4. Model danych i pliki
Nowe lub rozszerzone pliki danych:
1. `data/knowledge.json`:
   - `field_answers` (istniejace)
   - `copilot_recipes` (istniejace, rozszerzyc o metryki skutecznosci)
   - `agentic_playbooks` (nowe, sekwencje tool-calls dla state_signature)
2. `data/job_queue.jsonl` (nowe):
   - rekord kolejki: `job_id`, `url`, `source`, `score`, `status`, `last_attempt_at`.
3. `data/job_discovery_cache.json` (nowe):
   - cache wynikow wyszukiwania + timestamp + fingerprint zapytania.
4. `output/stuck_html/` (istniejace, dalej uzywane):
   - snapshoty HTML dla sytuacji stuck.
5. `output/agentic_traces/` (nowe):
   - skondensowana historia: obserwacja -> decyzja -> narzedzie -> efekt.

## 5. Interfejs narzedziowy LLM (Playwright Tool API)
LLM nie uruchamia dowolnego kodu. LLM wybiera tylko z bialej listy narzedzi:
1. `get_dom_snapshot`
2. `get_visible_fields`
3. `get_action_candidates`
4. `read_validation_messages`
5. `click_action(candidate_id)`
6. `type_into_field(field_id, value)`
7. `select_option(field_id, option_value)`
8. `set_checkbox(field_id, true_or_false)`
9. `set_file_input(field_id, file_path)`
10. `wait(ms)`
11. `scroll(px)`
12. `detect_login_or_captcha`
13. `take_screenshot`

Wymagania bezpieczenstwa:
1. Maksymalna liczba krokow fallback na oferte: np. 20.
2. Timeout fallback: np. 120s.
3. Blokada akcji ryzykownych: `discard`, `close application`, `logout`, `delete`.
4. Wymuszone stop conditions: sukces, twardy blad, brak progresu, wymagany handoff.

## 6. Detekcja zaciecia i aktywacja fallback
Warunki trigger fallback:
1. Powtarzany `state_signature` co najmniej 2 razy.
2. Powtarzana ta sama akcja co najmniej 3 razy.
3. Brak zmian w DOM/URL/validation przez okres X.
4. Brak kandydatow akcji w aktualnym kontekscie.
5. Wykrycie dynamicznych elementow niestandardowych, ktorych nie obsluguje deterministic runner.

Po triggerze:
1. Zapis snapshotu HTML.
2. Zebranie sygnalow strony.
3. Start petli agentic fallback.
4. Po niepowodzeniu fallback -> human handoff + auto resume.

## 7. Human handoff i automatyczne wznowienie
Tryb handoff:
1. Agent nie pyta o input w terminalu.
2. Agent czeka na zmiane w przegladarce (eventy click/change/input + zmiana fingerprintu stanu).
3. Po wykryciu zmiany wznowienie jest automatyczne.

Uczenie po interwencji:
1. Delta pol before/after zapisuje nowe odpowiedzi.
2. Delta akcji before/after mapowana jest na recipe dla danego `state_signature`.
3. Recipe dostaje score skutecznosci i licznik trafien.

## 8. Wyszukiwanie ofert po wymaganiach i CV (NOWE)
### 8.1 Cele modulu Job Discovery
1. Nie ograniczac sie tylko do saved jobs.
2. Samodzielnie znajdowac nowe oferty po kryteriach i dopasowaniu do profilu/CV.
3. Budowac ranking i kolejke do aplikowania.

### 8.2 Zrodla ofert
1. LinkedIn Search (query URL + filtry).
2. Saved jobs (dotychczasowe zrodlo, jako osobny channel).

### 8.3 Budowa zapytan (query builder)
Wejscia:
1. CV + profile + known answers.
2. Preferencje uzytkownika (rola, seniority, lokalizacja, typ umowy, widelek, remote/hybrid).
3. Slowa kluczowe wymagane i wykluczajace.

Wyjscia:
1. Lista query wariantow (np. 5-15 kombinacji).
2. Filtry LinkedIn: location, remote, date posted, easy apply, experience level.
3. Ograniczenia geograficzne (preferuj Polska, odrzuc role wymagajace relokacji poza PL).

### 8.4 Scoring i ranking ofert
Scoring wielokryterialny:
1. `skill_match_score` (dopasowanie umiejetnosci i technologii).
2. `experience_match_score` (seniority i rodzaj odpowiedzialnosci).
3. `constraint_score` (lokalizacja, umowa, jezyk, wymagania formalne).
4. `applyability_score` (szansa automatycznej aplikacji: easy/external complexity).
5. `priority_score` finalny = wazona suma.

Zasady:
1. Odrzucenie twarde: wymagania poza profilem (np. konieczna relokacja poza PL).
2. Odrzucenie miekkie: niski score ponizej progu.
3. Kolejka sortowana malejaco po `priority_score`.

### 8.5 Deduplikacja i anty-spam
1. Deduplikacja po `job_id` i URL.
2. Hash tresci ogloszenia, by wykrywac duplikaty re-postowane.
3. Cooldown ponownych prob dla `not_submitted` z limitem retry.
4. Unikanie wielokrotnego aplikowania na to samo ogloszenie.

### 8.6 Tryby run
1. `saved_only` (obecny).
2. `discovery_only` (tylko szukanie i ranking, bez aplikowania).
3. `discovery_and_apply` (szukanie + kolejkowanie + aplikowanie).

## 9. Szczegolowy plan implementacji (phased)
## Faza A: Stabilizacja fallback LLM Tool (priorytet P0)
1. Dodac warstwe `AgenticToolExecutor` w nowym pliku `src/agentic_tools.py`.
2. Dodac `AgenticFallbackController` w `src/linkedin_bot.py` lub nowym `src/agentic_fallback.py`.
3. Podlaczyc fallback do `_apply_easy` i `_apply_external` po triggerze stuck.
4. Dodac limity krokow i timeouty oraz blokady ryzykownych akcji.
5. Dodac obsluge `TargetClosedError` i recovery strony/kontekstu.

Artefakty:
1. Stabilny fallback dla co najmniej 3 klas formularzy.
2. Trace fallback zapisany do `output/agentic_traces/`.

## Faza B: Uczenie i reuse strategii (P0/P1)
1. Rozszerzyc `knowledge_store.py` o `agentic_playbooks`.
2. Dodac scoring recept: success_count, fail_count, last_used_at.
3. Przy nowym stuck najpierw probowac najlepszy playbook, potem LLM.
4. Dodac uczenie z interwencji czlowieka jako sekwencje narzedzi.
5. Dodac mechanizm "confidence threshold" dla automatycznego reuse.

Artefakty:
1. Rosnacy `playbook_hit_rate` miedzy kolejnymi runami.

## Faza C: Job Discovery (P1)
1. Dodac modul `src/job_discovery.py`:
   - query builder
   - fetch listy ofert z LinkedIn search
   - normalizacja rekordow
2. Dodac scoring discovery z LLM + heurystyki.
3. Dodac kolejke `data/job_queue.jsonl`.
4. Dodac integracje z `main.py`:
   - `--mode saved_only|discovery_only|discovery_and_apply`
   - `--discover-max N`
5. Dodac filtry preferencji z `.env`:
   - `DISCOVERY_ENABLED`
   - `DISCOVERY_KEYWORDS_INCLUDE`
   - `DISCOVERY_KEYWORDS_EXCLUDE`
   - `DISCOVERY_LOCATIONS`
   - `DISCOVERY_REMOTE_ONLY`
   - `DISCOVERY_DAYS_BACK`
   - `DISCOVERY_MAX_RESULTS`

Artefakty:
1. Dzialajacy pipeline znalezienia i kolejkowania ofert.
2. Raport rankingowy na koniec run.

## Faza D: Hardening i testy E2E (P1/P2)
1. Scenariusze testowe dla:
   - Easy Apply prosty
   - External z dynamicznymi polami
   - External z captcha/login/handoff
   - Discovery -> queue -> apply
2. Test regresji uczenia:
   - run #1 z interwencja
   - run #2 z mniejsza liczba handoff.
3. Test wydajnosci:
   - sredni czas na oferte
   - liczba krokow fallback.
4. Test bezpieczenstwa:
   - brak klikniec akcji zakazanych.

Artefakty:
1. Checklista QA i raport metryk.

## 10. Zmiany w kodzie per plik
Plan modyfikacji:
1. `src/main.py`:
   - nowe flagi trybow discovery
   - orchestracja kolejki
2. `src/config.py`:
   - nowe env i ustawienia discovery/fallback limits
3. `src/linkedin_bot.py`:
   - trigger fallback
   - wywolanie tool executor
   - handoff + auto resume
4. `src/llm_agent.py`:
   - planner narzedzi (next tool call)
   - scoring discovery
5. `src/knowledge_store.py`:
   - `agentic_playbooks`
   - metryki skutecznosci
6. `src/form_helper.py`:
   - expose id map fields dla narzedzi type/select/upload
7. `src/models.py`:
   - modele `DiscoveryJob`, `QueuedJob`, `Playbook`.
8. `src/job_discovery.py` (nowy):
   - discovery pipeline
9. `src/agentic_tools.py` (nowy):
   - bezpieczny executor narzedzi.
10. `src/agentic_fallback.py` (opcjonalnie nowy):
    - kontroler petli fallback.

## 11. Metryki i KPI
Minimalny zestaw:
1. `application_success_rate`
2. `fallback_trigger_rate`
3. `fallback_recovery_success_rate`
4. `human_handoff_rate`
5. `mean_steps_per_application`
6. `mean_time_per_application_sec`
7. `playbook_hit_rate`
8. `discovery_to_apply_conversion`

Cele po wdrozeniu:
1. Spadek handoff rate run-to-run.
2. Wzrost recovery success rate run-to-run.
3. Stabilne 3/3 testowe oferty bez twardego crash.

## 12. Ryzyka i mitigacje
Ryzyko:
1. Zmiana DOM LinkedIn/external ATS.
Mitigacja:
1. Warstwa semantyczna pol i akcji + fallback tool API.

Ryzyko:
1. Petle nieskonczone i "click storm".
Mitigacja:
1. Twarde limity krokow, timeout, detekcja braku progresu.

Ryzyko:
1. Nadmierna automatyzacja ryzykownych akcji.
Mitigacja:
1. Whitelista narzedzi i blacklist etykiet.

Ryzyko:
1. Slaba jakosc discovery (za duzo szumu).
Mitigacja:
1. Ranking wielokryterialny + twarde filtry preferencji.

## 13. Harmonogram wdrozenia
Proponowany harmonogram:
1. Dzien 1-2: Faza A (fallback tool executor + recovery control loop).
2. Dzien 3: Faza B (uczenie playbookow + reuse).
3. Dzien 4-5: Faza C (job discovery + queue + ranking).
4. Dzien 6: Faza D (testy E2E + tuning KPI).

## 14. Kryteria akceptacji release
Release v1.0 copilota uznajemy za gotowy gdy:
1. Run testowy na 3 ofertach przechodzi bez crash.
2. Co najmniej 1 trudny przypadek zostaje odzyskany przez LLM tool fallback.
3. Po interwencji czlowieka agent zapisuje nauke i wykorzystuje ja w kolejnym podobnym stanie.
4. Discovery zwraca sensowna kolejke ofert zgodna z profilem/CV i preferencjami.
5. Logi i metryki pozwalaja obiektywnie ocenic skutecznosc i regresje.
