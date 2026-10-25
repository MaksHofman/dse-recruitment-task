# Taksonomia marek alkoholi (DSE)

Rozwiązanie zadania rekrutacyjnego Data Solutions Engineer: lista marek alkoholi wraz z regułami
(`required` / `optional` / `optionalThreshold`) dla estymatora `urlkeywords.ctpl.dev`.

**Wynik:** [`MaksHofman.json`](MaksHofman.json), 82 reguły dla 78 marek. Każda reguła w estymatorze zwróciła
co najmniej jeden URL (sprawdzone skryptem `check.py`).

## Podejście: heurystyki + lokalny LLM

LLM nie generuje zapytań, tylko ocenia sens URL-i. Reguły buduje i wybiera deterministyczny kod,
więc wynik jest sprawdzalny i powtarzalny.

1. **Marki.** Ręczny seed w `brands.csv` (`brand,type,aliases,context,ambiguous`). Opcjonalnie `--expand`
   prosi LLM o dopisanie kolejnych; zmyślone marki odpadają, bo estymator nic dla nich nie zwróci.
2. **Dane.** Dla każdego zapisu marki (np. `jack-daniels`, `jack-daniel-s`) goła reguła idzie do estymatora.
   Ta sama reguła jest liczona lokalnie na świeżym korpusie URL-i z GDELT.
3. **Ocena.** LLM (Ollama, gemma3:12b) ocenia każdy URL dwoma niezależnymi zapytaniami; trafny tylko
   wtedy, gdy oba mówią „tak” (nazwisko, miejscowość, drużyna, giełda = nie).
4. **Drabina reguł.** Od najluźniejszej: sama marka, marka + słowa typu alkoholu, szerszy kontekst, kontekst
   z progiem 2. Wybierana jest najprostsza reguła bez szumu na ocenionych URL-ach. Słowa kontekstowe są
   ogólne (typ alkoholu), nigdy wyciągane z konkretnych stron, żeby reguła nie uczyła się próbki.
   Długie rdzenie (`whisk`, `distill`) idą w trybie `c`, krótkie słowa w `m` (inaczej `gin` łapie `origin`).
5. **Marki niejednoznaczne** (zwykłe słowa, np. Absolut, Corona) nie dostają gołej reguły i wymagają
   sygnału alkoholowego w URL-u.
6. **Weryfikacja w estymatorze.** Wybrana reguła musi zwrócić >= 1 URL i wszystkie zwrócone URL-e muszą
   być trafne. W wyniku są tylko reguły, które coś złapały.
7. **Ręczny przegląd.** Po przebiegu automatycznym przejrzałem URL-e zwrócone przez estymator
   (`evaluate.py`) i ręcznie zawęziłem reguły łapiące szum (m.in. Corona, Moët & Chandon, Grolsch)
   oraz groźne na produkcji (Beefeater, Four Roses, Evan Williams). Usunąłem pozycje, które nie są
   markami (Limoncello to typ trunku) albo nie miały trafnych URL-i.

## Uruchomienie

```bash
pip install -r requirements.txt
ollama pull gemma3:12b && ollama serve         # lokalny LLM

python taxonomy.py --name auto                 # brands.csv -> auto.json + report.html
python taxonomy.py --name auto --min-urls-amb 1  # luźniejszy próg dla marek niejednoznacznych
python evaluate.py auto.json                   # ręczny audyt URL-i -> evaluation.html
python check.py MaksHofman.json                  # test na estymatorze: puste reguły, podejrzane URL-e
python check.py MaksHofman.json --prune out.json  # zapisz tylko reguły z >= 1 URL-em
```

## Ograniczenia

- Oceniam tylko tekst URL-a, nie treść strony; LLM może się mylić.
- Estymator ma stałą próbkę, więc walidacja na nim jest optymistyczna. GDELT (newsy) mierzy głównie szum,
  nie pokrycie.
- Dla części marek estymator zwrócił tylko część trafień, więc nie wszystkie URL-e zweryfikowałem.
- Wysoki próg precyzji (zero szumu) skraca listę: wolałem mniej marek, ale czystych.
- Kilka przypadków granicznych zostawiłem świadomie (m.in. sklep Fireball, konkurs Jagermeistera,
  memorabilia Heinekena).

## Pliki

| plik | rola |
|---|---|
| `taxonomy.py` | pipeline: marki, estymator, LLM, drabina reguł |
| `evaluate.py` | ręczny audyt gotowego pliku reguł (raport HTML) |
| `check.py` | test pliku reguł na estymatorze, przycinanie pustych reguł |
| `brands.csv` | seed marek |
| `MaksHofman.json` | wynik końcowy |