#!/usr/bin/env python3
"""Sprawdza plik z regulami na estymatorze.

  python check.py mhofman.json                    # raport w konsoli
  python check.py mhofman.json --prune out.json   # zapisz tylko reguly, ktore zlapaly >=1 URL (ostrzezenia nie usuwaja regul - to Twoja decyzja)
  python check.py mhofman.json --all              # pokaz wszystkie URL-e, nie tylko 8 pierwszych

Dla kazdej reguly: liczba zwroconych URL-i, czy estymator zwrocil tylko czesc (quantity > liczba URL-i),
oraz OSTRZEZENIE, gdy wsrod URL-i jest cos z listy BAD (URL-e, ktore wczesniej uznalem za nietrafne).
"""
import argparse, json, re, sys, time
from concurrent.futures import ThreadPoolExecutor
import requests

EST = "https://urlkeywords.ctpl.dev/keywordURLs"
BAD = [  # fragmenty URL-i uznanych za nietrafne w poprzedniej ocenie
    "gatti_ciechi", "bardstown-bourbon-trail-house", "Fundacion-Cruzcampo", "yellow-tail-winemaker",
    "yellow-tail-iheartmedia", "dark-chocolates-with-limoncello", "milky-chance-grolsch",
    "modelo-beer-edible-image", "brewdog-pub-closure", "brewdog-partners-with", "brewdog-announces",
    "titos-soccer", "summer-x-titos", "titos-cooldown", "knob-creek-to-pursuit", "Keanu-Reeves", "timeline-bulmers", "samsummerditchdays", "corona-extra-pay"]


def ask(rule, est=None):
    body = {k: v for k, v in rule.items() if k != "id"}
    for i in range(3):
        try:
            r = requests.post(est or EST, data=json.dumps(body), timeout=60)
            r.raise_for_status()
            data = r.json()
            break
        except (requests.RequestException, ValueError):
            if i == 2:
                raise
            time.sleep(1.5 * (i + 1))
    urls, seen, qty = [], set(), 0
    for src in data.get("sources", {}).values():
        qty += int(src.get("quantity") or 0)
        for u in src.get("urls", []):
            m = re.match(r"^\[[^\]]*\]\((https?://[^)\s]+)\)$", u.strip())
            u = (m.group(1) if m else u).strip().split("#")[0]
            if u.startswith("http") and u not in seen:
                seen.add(u)
                urls.append(u)
    return urls, qty


def describe(r):
    d = " & ".join(k["keyword"] for k in r["required"])
    if r.get("optional"):
        d += f"  +  min {r['optionalThreshold']} z [{', '.join(k['keyword'] for k in r['optional'])}]"
    return d


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file")
    ap.add_argument("--prune", default="")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--url", default=EST)
    a = ap.parse_args()
    rules = json.load(open(a.file, encoding="utf-8"))["results"]

    def work(r):
        try:
            return r, *ask(r, a.url), None
        except Exception as e:
            return r, [], 0, str(e)[:100]

    with ThreadPoolExecutor(max_workers=4) as ex:
        res = list(ex.map(work, rules))

    empty, flagged, errors, keep = [], [], [], []
    for r, urls, qty, err in res:
        bad = [u for u in urls if any(b.lower() in u.lower() for b in BAD)]
        tag = "BLAD" if err else ("PUSTA" if not urls else ("OSTRZEZENIE" if bad else "ok"))
        part = " (czesc - estymator zwrocil wiecej niz pokazal)" if qty > len(urls) else ""
        print(f"[{tag}] {r['id']}: {describe(r)[:110]} -> {len(urls)} URL-i{part}")
        for u in (urls if a.all else urls[:8]):
            print("      " + ("!! " if u in bad else "   ") + u[:140])
        if err:
            errors.append(r["id"]); print("      blad:", err)
        elif not urls:
            empty.append(r)
        elif bad:
            flagged.append((r["id"], bad))
        if urls:
            keep.append(r)
    ids_all, ids_keep = {r["id"] for r in rules}, {r["id"] for r in keep}
    print(f"\nreguly: {len(rules)} | ok: {len(keep)} | puste: {len(empty)} | z ostrzezeniem: {len(flagged)} | bledy: {len(errors)}")
    print(f"marki: {len(ids_all)} -> po przycieciu {len(ids_keep)}")
    for r in empty:
        print("  PUSTA:", r["id"], "-", describe(r)[:90])
    for i, b in flagged:
        print("  PODEJRZANE:", i, *[x[:90] for x in b], sep="\n      ")
    if a.prune:
        json.dump({"results": keep}, open(a.prune, "w", encoding="utf-8"), ensure_ascii=False)
        print("zapisano", a.prune)


if __name__ == "__main__":
    main()
