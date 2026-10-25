#!/usr/bin/env python3
"""Taksonomia marek alkoholi dla estymatora urlkeywords.ctpl.dev - cały pipeline w jednym pliku.

  python taxonomy.py --name mhofman                 # brands.csv -> mhofman.json + report.html
  python taxonomy.py --name mhofman --expand        # najpierw LLM dopisuje kolejne marki do brands.csv
  python taxonomy.py --name mhofman --only "Absolut,Martini"

Dla każdego zapisu marki (slug):
  1. estymator: goła reguła -> URL-e;  korpus świeżych URL-i (GDELT): trafienia tej samej reguły
  2. lokalny LLM (Ollama) ocenia KAŻDY URL: czy strona jest o tej marce alkoholu
  3. drabina reguł: bare -> słowa typu alkoholu -> szerszy kontekst -> kontekst x2; wybieramy
     najprostszą, która na ocenionych URL-ach nie ma szumu (żadnych słów uczonych z próbki)
  4. weryfikacja: wybraną regułę pytamy w estymatorze; WSZYSTKIE zwrócone URL-e muszą być trafne
     i co najmniej jeden musi istnieć. Porażka -> nowe oceny trafiają do puli, następna reguła.
"""
import argparse, csv, html, io, json, os, re, sys, threading, time, unicodedata, zipfile
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from pathlib import Path

import requests

EST = "https://urlkeywords.ctpl.dev/keywordURLs"
OLLAMA = os.getenv("OLLAMA_URL", "http://localhost:11434")
MODEL = os.getenv("OLLAMA_MODEL", "gemma3:12b")
GDELT = "http://data.gdeltproject.org/gdeltv2/"
CACHE = Path("cache")

# słowa kontekstowe: celowo OGÓLNE (typ alkoholu), nigdy wyciągane z konkretnych stron.
# Długie, charakterystyczne rdzenie idą w trybie "c" (łapią whiskyliquorstore.com, hendricksgin.com,
# wodkaexporte); krótkie słowa tylko w trybie "m" (inaczej gin -> origin, rum -> forum).
STEMS = {"whisk", "bourbon", "vodka", "wodka", "tequila", "mezcal", "cognac", "liqueur", "likier", "champagne",
         "prosecco", "brewery", "distill", "vermouth", "wermut", "cerveza", "birra", "winery", "vineyard"}
WEAK = {"cocktail", "cocktails", "drink", "drinks", "shot", "shots", "tonic", "spritz", "brut", "sparkling"}
TYPE_WORDS = {
    "vodka": "vodka wodka cocktail cocktails", "whisky": "whisk bourbon scotch malt cocktail",
    "rum": "rum rhum cocktail cocktails", "gin": "gin tonic cocktail cocktails",
    "tequila": "tequila mezcal margarita cocktail", "cognac": "cognac brandy",
    "liqueur": "liqueur likier cocktail cocktails spritz", "vermouth": "vermouth wermut aperitivo aperitif",
    "champagne": "champagne sparkling prosecco brut", "beer": "beer piwo lager stout pils brewery browar bier birra cerveza",
    "wine": "wine wines wino vino wein winery vineyard", "cider": "cider cidre cydr",
    "other": "alcohol alkohol cocktail cocktails"}
BROAD = ("vodka wodka whisk bourbon gin tequila cognac brandy liqueur vermouth wine beer piwo lager "
         "champagne prosecco cider spirits liquor alcohol alkohol brewery distill drink drinks").split()
# bramka leksykalna dla marek niejednoznacznych: URL musi mieć sygnał alkoholowy
EVID_STEMS = sorted(STEMS | {"cocktail", "liquor", "tadim", "single-malt", "singlemalt"})
EVID_WORDS = {"gin", "rum", "wine", "wines", "beer", "beers", "ale", "ipa", "lager", "stout", "pils", "brandy", "scotch",
              "malt", "drink", "drinks", "spritz", "vino", "wein", "bier", "piwo", "alcohol", "alkohol", "spirits",
              "750ml", "700ml", "70cl", "375ml", "50ml", "1l", "abv"}
_TOK = re.compile(r"[a-z0-9]+")
# URL o giełdzie/modzie itp. nigdy nie jest "o marce alkoholu" (np. remy-cointreau-chart, veuve-clicquot-polo-outfits)
VETO = {"stock", "stocks", "equities", "shares", "aktie", "kurs", "investing", "marketscreener", "zonebourse",
        "klsescreener", "earnings", "analystenerwartungen", "polo", "outfit", "outfits"}


def vetoed(url):
    return bool(VETO & set(_TOK.findall(url.lower())))


def evidence(url):
    u = url.lower()
    return any(s in u for s in EVID_STEMS) or bool(EVID_WORDS & set(_TOK.findall(u)))


def kw(word):
    return {"keyword": word, "mode": "c" if word in STEMS else "m"}


# ------------------------------------------------------------------ marki i sluga
def slugify(name, apos_sep=False):
    s = re.sub(r"['’`]", " " if apos_sep else "", name.replace("&", " "))
    s = s.translate(str.maketrans({"ł": "l", "Ł": "L", "ø": "o", "Ø": "O", "đ": "d", "ß": "ss"}))
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def variants(b):
    own = [] if (b.get("ambiguous") or "").strip().lower() == "skip" else [slugify(b["brand"]), slugify(b["brand"], True)]
    out = own + [slugify(a) for a in (b.get("aliases") or "").split(";")]
    return [s for s in dict.fromkeys(out) if len(s) >= 3]


def csv_list(v):
    return [x.strip() for x in re.split(r"[;,\s]+", v or "") if x.strip()]


def load_brands(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        return [r for r in csv.DictReader(f) if r.get("brand")]


# ------------------------------------------------------------------ matcher (kopia logiki estymatora)
@lru_cache(maxsize=None)
def _rx(kw, mode):
    core = "[^a-z0-9]+".join(map(re.escape, filter(None, kw.lower().split("-"))))   # "-" = separator
    return re.compile(rf"(?<![a-z0-9]){core}(?![a-z0-9])" if mode == "m" else core)


def hit(rule, url):
    u = url.lower()
    if not all(_rx(k["keyword"], k["mode"]).search(u) for k in rule["required"]):
        return False
    opt = rule.get("optional")
    return not opt or sum(bool(_rx(k["keyword"], k["mode"]).search(u)) for k in opt) >= rule["optionalThreshold"]


def make_rule(slug, words=(), thr=1):
    rule = {"required": [{"keyword": slug, "mode": "m"}]}
    words = [w for w in dict.fromkeys(words) if w != slug]
    if words:
        rule["optional"] = [kw(w) for w in words]
        rule["optionalThreshold"] = min(thr, len(words))
    return rule


def ladder(slug, b, ambiguous):
    """Od najluźniejszej. Marka niejednoznaczna nigdy nie dostaje `bare`; słabe słowa (cocktail, drink...) wymagają progu 2."""
    tw = (TYPE_WORDS.get(b.get("type")) or TYPE_WORDS["other"]).split()
    strong = [w for w in tw if w not in WEAK]
    ctx = [w for w in csv_list(b.get("context")) if w != slug]
    broad_strong = [w for w in BROAD if w not in WEAK]
    c = []
    if not ambiguous:
        c.append(("bare", make_rule(slug)))
    if ctx:
        c.append(("context", make_rule(slug, ctx)))
        c.append(("context+type", make_rule(slug, ctx + strong)))
    if strong:
        c.append(("type", make_rule(slug, strong)))
    c.append(("type+weak x2", make_rule(slug, tw, 2)))
    c.append(("broad", make_rule(slug, broad_strong + strong, 1 if not ambiguous else 2)))
    c.append(("broad+weak x2", make_rule(slug, BROAD + tw, 2)))
    return list({repr(r): (n, r) for n, r in c}.values())


# ------------------------------------------------------------------ estymator
def ask(rule):
    """-> (urls, quantity). Estymator nie zna pola `id`."""
    body = {k: v for k, v in rule.items() if k != "id"}
    for i in range(3):
        try:
            r = requests.post(EST, data=json.dumps(body), timeout=60)
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
            m = re.match(r"^\[[^\]]*\]\((https?://[^)\s]+)\)$", u.strip())      # czasem markdown [txt](url)
            u = (m.group(1) if m else u).strip().split("#")[0]
            if u.startswith("http") and u not in seen:
                seen.add(u)
                urls.append(u)
    return urls, qty


# ------------------------------------------------------------------ lokalny LLM (Ollama)
def chat(prompt, schema):
    try:
        r = requests.post(f"{OLLAMA}/api/chat", timeout=600, json={
            "model": MODEL, "stream": False, "format": schema, "keep_alive": "30m",
            "messages": [{"role": "user", "content": prompt}], "options": {"temperature": 0, "num_ctx": 8192}})
        r.raise_for_status()
        return json.loads(r.json()["message"]["content"])
    except (requests.RequestException, ValueError, KeyError):
        return None


def llm_ready():
    try:
        names = {m["name"] for m in requests.get(f"{OLLAMA}/api/tags", timeout=5).json()["models"]}
    except (requests.RequestException, ValueError, KeyError):
        return "Ollama nie odpowiada (uruchom: ollama serve)"
    return "" if MODEL in names or f"{MODEL}:latest" in names else f"brak modelu: ollama pull {MODEL}"


_BOOL = {"type": "object", "required": ["answer"], "properties": {"answer": {"type": "boolean"}}}
_MEAN = {"type": "object", "required": ["meaning", "is_alcohol_brand"], "properties": {
    "meaning": {"type": "string"}, "is_alcohol_brand": {"type": "boolean"}}}
_lock, VERDICTS = threading.Lock(), {}          # cache werdyktów: "v2|marka|url" -> bool
JUDGE_CAP = 60                                   # max URL-i ocenianych na jedną markę (najpierw te z sygnałem alkoholowym)


def _vkey(b, u):
    return f"v2|{b['brand']}|{u}"


def _judge_one(b, url, ambiguous):
    """Dwa niezależne przebiegi (inny prompt); trafny tylko gdy OBA mówią tak. Brak odpowiedzi = False."""
    name, typ = b["brand"], b.get("type") or "alcohol"
    first = chat(
        f'URL: {url}\nThe URL contains the word "{name}". First say in a few words what that word means IN THIS URL '
        f'(a person, place, sports team, company in another field, ordinary word, film, product of another kind, '
        f'or the {typ} brand "{name}"). Then set is_alcohol_brand=true ONLY if it is the alcohol brand and the page is '
        'about that brand\'s products, prices, shops, reviews, recipes or cocktails featuring it, or news about the brand '
        'itself. A cocktail that merely shares the name, stock news about a parent company, or a passing mention = false.',
        _MEAN)
    second = chat(
        f'Is this page specifically about the {typ} brand "{name}" (the alcoholic drink), and not about anything else '
        f'that shares the name? When unsure answer false.\nURL: {url}', _BOOL)
    if first is None or second is None:
        return None
    return bool(first.get("is_alcohol_brand")) and bool(second.get("answer"))


def judge(b, urls, ambiguous=False):
    """{url: bool}. Niejednoznaczna marka: dodatkowo bramka leksykalna (URL musi mieć sygnał alkoholowy)."""
    todo = [u for u in dict.fromkeys(urls) if _vkey(b, u) not in VERDICTS and not vetoed(u)]
    todo.sort(key=lambda u: not evidence(u))
    for u in todo[:JUDGE_CAP]:
        r = _judge_one(b, u, ambiguous)
        if r is not None:
            with _lock:
                VERDICTS[_vkey(b, u)] = r
    out = {}
    for u in urls:
        v = False if vetoed(u) else VERDICTS.get(_vkey(b, u))
        if v is not None:
            out[u] = v and (not ambiguous or evidence(u) or any(
                _rx(k, "m").search(u.lower()) for k in csv_list(b.get("context"))))
    return out                                     # URL-e bez oceny (ponad JUDGE_CAP) nie wchodzą do puli


def _has_other_meaning(b, slug):
    """Wstępna niejednoznaczność: kolumna `ambiguous` > fraza wielowyrazowa (nie) > wordfreq dla pojedynczego słowa.
    Dodatkowo process() sprawdza empirycznie (odsetek nietrafnych URL-i w próbce estymatora)."""
    v = (b.get("ambiguous") or "").strip().lower()
    if v in ("1", "true", "yes", "tak", "skip"):
        return True
    if v in ("0", "false", "no", "nie") or "-" in slug:
        return False
    try:
        from wordfreq import zipf_frequency
        return max(zipf_frequency(slug, l) for l in ("en", "pl", "de", "es", "fr", "it", "pt", "nl")) >= 3.2
    except ImportError:
        return False


def expand(path, per_call=40):
    """LLM wymienia znane marki; zmyślone i tak odpadną, bo estymator nie zwróci dla nich URL-i."""
    schema = {"type": "object", "required": ["brands"], "properties": {"brands": {"type": "array", "items": {"type": "string"}}}}
    rows = load_brands(path)
    seen = {s for r in rows for s in variants(r)}
    added = 0
    for typ in [t for t in TYPE_WORDS if t != "other"]:
        for scope in ("worldwide", "Poland and Central Europe", "USA", "Spain, Italy and Latin America", "Asia"):
            data = chat(f"List {per_call} well-known {typ} brands sold in {scope}. Brand names only, no producers, "
                        'no generic words. Return {"brands":["..."]}.', schema)
            for name in (data or {}).get("brands", []):
                s = slugify(str(name))
                if len(s) >= 3 and s not in seen:
                    seen.add(s)
                    rows.append({"brand": str(name).strip(), "type": typ, "aliases": "", "context": "", "ambiguous": ""})
                    added += 1
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["brand", "type", "aliases", "context", "ambiguous"], extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    print(f"expand: +{added} marek -> {path}")


# ------------------------------------------------------------------ świeży korpus URL-i (GDELT)
def load_fresh(n_files):
    f = CACHE / "gdelt_urls.txt"
    if n_files and not f.exists():
        lines = requests.get(GDELT + "masterfilelist.txt", timeout=120).text.splitlines()
        files = [l.split()[-1] for l in lines if l.strip().endswith(".export.CSV.zip")][-n_files:]
        urls = set()
        for i, u in enumerate(files, 1):
            try:
                z = zipfile.ZipFile(io.BytesIO(requests.get(u, timeout=120).content))
                for row in z.open(z.namelist()[0]).read().decode("utf-8", "ignore").splitlines():
                    last = row.split("\t")[-1].strip()                          # ostatnia kolumna = SOURCEURL
                    if last.startswith("http"):
                        urls.add(last.split("#")[0])
            except Exception as e:
                print("GDELT pominięto", u, e)
            if i % 20 == 0:
                print(f"GDELT {i}/{len(files)}, {len(urls)} URL-i")
        f.write_text("\n".join(sorted(urls)), encoding="utf-8")
    return f.read_text(encoding="utf-8").split() if f.exists() else []


# ------------------------------------------------------------------ wybór reguły i przebieg dla jednej marki
def pick(cands, pool, est_urls, tried, max_noise):
    """Najprostsza reguła bez szumu (na całej puli), z >= 1 trafnym URL-em z estymatora i recallem >= 90% najlepszej."""
    rows = []
    for name, rule in cands:
        urls = [u for u in pool if hit(rule, u)]
        good = sum(pool[u] for u in urls)
        ok = (any(pool[u] and u in est_urls for u in urls) and (len(urls) - good) <= max_noise * len(urls)
              and repr(rule) not in tried)
        rows.append({"name": name, "rule": rule, "good": good, "bad": len(urls) - good, "ok": ok})
    clean = [r for r in rows if r["ok"]]
    if not clean:
        return None, rows
    best = max(r["good"] for r in clean)
    return min((r for r in clean if r["good"] >= 0.9 * best),
               key=lambda r: (len(r["rule"].get("optional", [])), -r["good"])), rows


def process(b, slug, fresh, a):
    out = {"id": b["brand"], "slug": slug, "status": "rejected", "reason": "", "rule": None}
    bare = make_rule(slug)
    urls, qty = ask(bare)
    if not urls:
        out.update(status="no_results", reason="estymator nie zwrócił żadnego URL-a")
        return out
    est_urls = set(urls)
    fresh_hits = [u for u in fresh if hit(bare, u)]
    amb = _has_other_meaning(b, slug)
    pool = judge(b, urls + fresh_hits, amb)
    if not amb and len(pool) >= 3 and sum(not v for v in pool.values()) >= 0.3 * len(pool):
        amb = True                                          # empirycznie: >=30% URL-i bez związku z alkoholem
        pool = judge(b, urls + fresh_hits, amb)
    out.update(pool=pool, bare=bare, fresh_bare=len(fresh_hits), ambiguous=amb)
    out["partial"] = qty > len(urls)                       # estymator zwrócił tylko część trafień
    tried = set()
    for rnd in range(1, a.max_rounds + 1):
        p, rows = pick(ladder(slug, b, amb), pool, est_urls, tried, a.max_noise)
        out.setdefault("ladder", [(r["name"], r["good"], r["bad"]) for r in rows])
        if p is None:
            out["reason"] = "żadna reguła nie jest czysta: " + "; ".join(f"{r['name']} {r['good']}+/{r['bad']}-" for r in rows)
            return out
        res, _ = ask(p["rule"])
        pool.update(judge(b, [u for u in res if u not in pool], amb))
        est_urls |= set(res)
        bad = [u for u in res if not pool.get(u, False)]      # brak oceny = zły (ostrożnie)
        good = [u for u in res if pool.get(u, False)]
        if amb and len(good) < a.min_urls_amb:
            out["reason"] = f"marka niejednoznaczna: tylko {len(good)} trafny URL (min {a.min_urls_amb})"
        elif good and len(bad) <= a.max_noise * len(res):
            fr = [u for u in fresh_hits if hit(p["rule"], u)]
            out.update(status="ok", rule=p["rule"], name=p["name"], good=good, bad=bad, fresh_n=len(fr),
                       fresh_good=sum(pool.get(u, False) for u in fr), reason=f"{p['name']}: {len(good)} trafnych / {len(res)} z estymatora, "
                       f"świeże trafienia: {len(fr)}")
            return out
        tried.add(repr(p["rule"]))
        if not out["reason"].startswith("marka niejednoznaczna"):
            out["reason"] = f"weryfikacja w estymatorze nie przeszła ({len(bad)} złych z {len(res)})"
    return out


def drop_redundant(outs):
    """Wariant zapisu zbędny, gdy inna reguła tej marki łapie wszystkie jego URL-e."""
    groups = {}
    for o in outs:
        if o["status"] == "ok":
            groups.setdefault(o["id"], []).append(o)
    for g in groups.values():
        kept = []
        for o in sorted(g, key=lambda o: -len(o["good"])):
            if any(set(o["good"]) <= set(k["good"]) for k in kept):
                o["status"], o["reason"] = "redundant", "URL-e łapie już inna reguła tej marki"
            else:
                kept.append(o)


CSS = """:root{--bg:#fff;--fg:#1d2430;--mut:#667085;--card:#f6f8fa;--bd:#d8dee6;--ok:#137a3d;--bad:#c0262d;--warn:#9a6700;--mark:#ffe58f}
@media(prefers-color-scheme:dark){:root{--bg:#14171c;--fg:#e6e9ee;--mut:#98a2b3;--card:#1c2128;--bd:#323944;--ok:#4cc380;--bad:#ff7b81;--warn:#e3b341;--mark:#5c4a00}}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,sans-serif}
header{position:sticky;top:0;background:var(--bg);border-bottom:1px solid var(--bd);padding:10px 16px;z-index:5}
h1{margin:0 0 6px;font-size:18px}.meta{color:var(--mut);font-size:12px}
.bar{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:8px}
button,input{font:inherit;padding:4px 10px;border:1px solid var(--bd);border-radius:6px;background:var(--card);color:var(--fg)}
button.on{border-color:var(--fg);font-weight:600}main{max-width:1100px;margin:0 auto;padding:12px 16px}
details{background:var(--card);border:1px solid var(--bd);border-radius:8px;margin:8px 0;padding:6px 12px}
summary{cursor:pointer;display:flex;gap:10px;align-items:baseline;flex-wrap:wrap}summary b{font-size:15px}
.tag{font-size:11px;padding:1px 7px;border-radius:10px;border:1px solid currentColor}
.ok{color:var(--ok)}.rejected,.error{color:var(--bad)}.no_results,.redundant{color:var(--warn)}
.rule{font-family:ui-monospace,monospace;background:var(--bg);border:1px solid var(--bd);border-radius:6px;padding:4px 8px;margin:6px 0;word-break:break-word}
ul{list-style:none;padding:0;margin:4px 0}li{margin:2px 0;word-break:break-all}li.y::before{content:"\\2713 ";color:var(--ok)}li.n::before{content:"\\2717 ";color:var(--bad)}
mark{background:var(--mark);color:inherit;border-radius:2px}a{color:inherit}table{border-collapse:collapse;margin:6px 0}
td,th{border:1px solid var(--bd);padding:2px 8px;text-align:left}.mut{color:var(--mut)}"""
JS = """const q=document.getElementById('q');let st='all';
function f(){document.querySelectorAll('details').forEach(d=>{const t=d.dataset.s,ok=st=='all'||t==st||(st=='bad'&&t!='ok');
d.style.display=ok&&d.textContent.toLowerCase().includes(q.value.toLowerCase())?'':'none'})}
document.querySelectorAll('button').forEach(b=>b.onclick=()=>{st=b.dataset.f;document.querySelectorAll('button').forEach(x=>x.classList.toggle('on',x==b));f()});q.oninput=f;"""


def _hl(url, rule):
    """URL z podświetlonymi keywordami reguły."""
    low = url.lower()
    if not rule or len(low) != len(url):
        return html.escape(url)
    mark = [False] * len(url)
    for k in rule["required"] + rule.get("optional", []):
        for m in _rx(k["keyword"], k["mode"]).finditer(low):
            for i in range(*m.span()):
                mark[i] = True
    out, i = "", 0
    while i < len(url):
        j = i
        while j < len(url) and mark[j] == mark[i]:
            j += 1
        seg = html.escape(url[i:j])
        out += f"<mark>{seg}</mark>" if mark[i] else seg
        i = j
    return out


def _li(url, ok, rule):
    return f'<li class="{"y" if ok else "n"}"><a href="{html.escape(url)}" target="_blank" rel="noopener">{_hl(url, rule)}</a></li>'


def _describe(r):
    d = " &amp; ".join(html.escape(k["keyword"]) for k in r["required"])
    if r.get("optional"):
        d += f" + min {r['optionalThreshold']} z [{html.escape(', '.join(k['keyword'] for k in r['optional']))}]"
    return d


def write_report(path, outs, meta):
    n = {s: sum(o["status"] == s for o in outs) for s in ("ok", "rejected", "no_results", "redundant", "error")}
    brands_ok = len({o["id"] for o in outs if o["status"] == "ok"})
    cards = []
    for o in sorted(outs, key=lambda o: (o["status"] != "ok", o["id"], o["slug"])):
        pool, rule = o.get("pool", {}), o.get("rule") or o.get("bare")
        body = ""
        if o["status"] == "ok":
            body += f'<div class="rule">{_describe(o["rule"])}</div>'
            body += f'<div class="mut">estymator: {len(o["good"])} trafnych, {len(o["bad"])} złych | świeże trafienia reguły: {o["fresh_n"]} (trafnych {o["fresh_good"]})</div>'
            body += "<ul>" + "".join(_li(u, True, rule) for u in o["good"][:25]) + "".join(_li(u, False, rule) for u in o["bad"][:10]) + "</ul>"
        else:
            if o.get("ladder"):
                body += "<table><tr><th>reguła</th><th>trafne</th><th>złe</th></tr>" + "".join(
                    f"<tr><td>{html.escape(n_)}</td><td>{g}</td><td>{b_}</td></tr>" for n_, g, b_ in o["ladder"]) + "</table>"
            if pool:
                items = sorted(pool.items(), key=lambda kv: kv[1])[:40]
                body += f'<div class="mut">oceny LLM dla gołej reguły ({len(pool)} URL-i, pokazano {len(items)}):</div><ul>' + "".join(
                    _li(u, v, o["bare"]) for u, v in items) + "</ul>"
        if o.get("partial"):
            body += '<div class="warn">uwaga: estymator zwrócił tylko część trafień</div>'
        cards.append(f'<details data-s="{o["status"]}"{" open" if False else ""}><summary><b>{html.escape(o["id"])}</b>'
                     f'<span class="mut">{html.escape(o["slug"])}</span><span class="tag {o["status"]}">{o["status"]}</span>'
                     f'<span class="mut">{html.escape(o["reason"][:140])}</span></summary>{body}</details>')
    page = (f'<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>Raport: taksonomia marek</title><style>{CSS}</style><header><h1>Raport: taksonomia marek alkoholi</h1>'
            f'<div class="meta">{meta["when"]} | model {html.escape(meta["model"])} | świeżych URL-i: {meta["fresh"]} | '
            f'max szum: {meta["max_noise"]} | <b>{n["ok"]} reguł ({brands_ok} marek)</b>, odrzucone: {n["rejected"]}, '
            f'bez wyników: {n["no_results"]}, zbędne: {n["redundant"]}, błędy: {n["error"]}</div>'
            f'<div class="bar"><button data-f="all" class="on">wszystkie</button><button data-f="ok">ok</button>'
            f'<button data-f="bad">odrzucone</button><input id="q" placeholder="szukaj marki lub URL-a"></div></header>'
            f'<main>{"".join(cards)}</main><script>{JS}</script>')
    Path(path).write_text(page, encoding="utf-8")


def main():
    t0 = time.perf_counter()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--name", required=True, help="np. jkowalski -> jkowalski.json")
    ap.add_argument("--brands", default="brands.csv")
    ap.add_argument("--only", default="", help='np. "Absolut,Martini"')
    ap.add_argument("--expand", action="store_true", help="LLM dopisuje kolejne marki do brands.csv")
    ap.add_argument("--fresh", type=int, default=96, help="pliki GDELT (15 min każdy); 0 = bez świeżego korpusu")
    ap.add_argument("--max-noise", type=float, default=0.0, help="dopuszczalny odsetek złych URL-i (0 = zero szumu)")
    ap.add_argument("--max-rounds", type=int, default=3)
    ap.add_argument("--min-urls-amb", type=int, default=2, help="min trafnych URL-i dla marki niejednoznacznej")
    ap.add_argument("--workers", type=int, default=2)
    a = ap.parse_args()

    if err := llm_ready():
        sys.exit(err)
    CACHE.mkdir(exist_ok=True)
    cf = CACHE / "verdicts.json"
    if cf.exists():
        VERDICTS.update(json.loads(cf.read_text(encoding="utf-8")))
    if a.expand:
        expand(a.brands)
    brands = load_brands(a.brands)
    if a.only:
        want = {x.strip().lower() for x in a.only.split(",")}
        brands = [b for b in brands if b["brand"].lower() in want]
    fresh = load_fresh(a.fresh)
    targets = [(b, s) for b in brands for s in variants(b)]
    print(f"marek: {len(brands)}, zapisów: {len(targets)}, świeżych URL-i: {len(fresh)}, model: {MODEL}")

    def work(t):
        try:
            o = process(*t, fresh, a)
        except Exception as e:                                           # sieć itp. - nie wywracaj całości
            o = {"id": t[0]["brand"], "slug": t[1], "status": "error", "reason": str(e)[:120], "rule": None}
        with _lock:
            cf.write_text(json.dumps(VERDICTS), encoding="utf-8")
        print(f"{o['id']} ({o['slug']}): {o['status']} - {o['reason'][:100]}", flush=True)
        return o

    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        outs = list(ex.map(work, targets))
    drop_redundant(outs)
    ok = [o for o in outs if o["status"] == "ok"]
    Path(f"{a.name}.json").write_text(json.dumps({"results": [{"id": o["id"], **o["rule"]} for o in ok]},
                                                 ensure_ascii=False), encoding="utf-8")
    write_report("report.html", outs, {"when": datetime.now().strftime("%Y-%m-%d %H:%M"), "model": MODEL,
                                        "fresh": len(fresh), "max_noise": a.max_noise})
    print(f"\nreguł: {len(ok)} (marek: {len({o['id'] for o in ok})}) -> {a.name}.json | raport: report.html")
    m, s = divmod(int(time.perf_counter() - t0), 60)
    print(f"Czas: {m} min {s} s")

if __name__ == "__main__":
    main()