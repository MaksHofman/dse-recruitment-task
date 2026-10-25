#!/usr/bin/env python3
"""Ręczna ocena gotowego pliku reguł na ESTYMATORZE z zadania (niezależna od taxonomy.py).

  python evaluate.py mhofman.json                    # -> evaluation.html
  python evaluate.py mhofman.json --only "Absolut,Martini"

Każda reguła z pliku jest wysyłana do estymatora; raport pokazuje wszystkie zwrócone URL-e (podświetlone
keywordy, klikalne linki). Przy URL-u zaznaczasz "zły", przy marce "przejrzane" - zaznaczenia nic nie
zmieniają w regułach, tylko liczą statystyki na górze.
Oceny zapisują się w przeglądarce (localStorage) i można je pobrać / wczytać jako JSON.
"""
import argparse, hashlib, json, sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from taxonomy import _rx, ask, hit


def spans(url, rule):
    low = url.lower()
    if len(low) != len(url):
        return []
    out = []
    for k in rule["required"] + rule.get("optional", []):
        out += [list(m.span()) for m in _rx(k["keyword"], k["mode"]).finditer(low)]
    return sorted(out)


def describe(r):
    d = " & ".join(k["keyword"] for k in r["required"])
    if r.get("optional"):
        d += f"  +  min {r['optionalThreshold']} z [{', '.join(k['keyword'] for k in r['optional'])}]"
    return d


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file")
    ap.add_argument("--out", default="evaluation.html")
    ap.add_argument("--only", default="")
    ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args()

    raw = open(a.file, encoding="utf-8").read()
    rules = json.loads(raw)["results"]
    if a.only:
        want = {x.strip().lower() for x in a.only.split(",")}
        rules = [r for r in rules if r["id"].lower() in want]

    def run(r):
        try:
            return ask(r) + ("",)
        except Exception as e:
            return [], 0, str(e)[:120]
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        results = list(ex.map(run, rules))

    brands = {}
    for r, (urls, qty, err) in zip(rules, results):
        b = brands.setdefault(r["id"], {"id": r["id"], "rules": [], "urls": [], "partial": False, "errors": []})
        b["rules"].append(describe(r))
        b["partial"] |= qty > len(urls)
        if err:
            b["errors"].append(err)
        seen = {x["u"] for x in b["urls"]}
        for u in urls:
            if u not in seen:
                seen.add(u)
                b["urls"].append({"u": u, "s": spans(u, r)})
        print(f"{r['id']:26s} {len(urls):4d}" + (f"  BŁĄD {err}" if err else ""))
    data = sorted(brands.values(), key=lambda b: b["id"].lower())
    meta = {"when": datetime.now().strftime("%Y-%m-%d"), "file": a.file, "rules": len(rules),
            "id": hashlib.sha1(raw.encode()).hexdigest()[:10]}
    page = TEMPLATE.replace("__DATA__", json.dumps(data, ensure_ascii=False).replace("</", "<\\/")) \
                   .replace("__META__", json.dumps(meta, ensure_ascii=False))
    open(a.out, "w", encoding="utf-8").write(page)
    print(f"\n{len(data)} marek, {sum(len(b['urls']) for b in data)} URL-i -> {a.out}")


TEMPLATE = r"""<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Ocena reguł na estymatorze</title>
<style>
:root{--bg:#fff;--fg:#1d2430;--mut:#667085;--card:#f6f8fa;--bd:#d8dee6;--ok:#137a3d;--bad:#c0262d;--mark:#ffe58f}
@media(prefers-color-scheme:dark){:root{--bg:#14171c;--fg:#e6e9ee;--mut:#98a2b3;--card:#1c2128;--bd:#323944;--ok:#4cc380;--bad:#ff7b81;--mark:#5c4a00}}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,sans-serif}
header{position:sticky;top:0;background:var(--bg);border-bottom:1px solid var(--bd);padding:10px 16px;z-index:5}
h1{margin:0 0 6px;font-size:18px}.stats{display:flex;gap:18px;flex-wrap:wrap;margin:6px 0}
.stat b{display:block;font-size:20px}.stat span{color:var(--mut);font-size:12px}.big b{color:var(--ok);font-size:26px}
.bar{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
button,input[type=text]{font:inherit;padding:4px 10px;border:1px solid var(--bd);border-radius:6px;background:var(--card);color:var(--fg);cursor:pointer}
button.on{border-color:var(--fg);font-weight:600}textarea{width:100%;height:150px;margin-top:8px;font:12px ui-monospace,monospace;background:var(--card);color:var(--fg);border:1px solid var(--bd)}
main{max-width:1100px;margin:0 auto;padding:12px 16px}details{background:var(--card);border:1px solid var(--bd);border-radius:8px;margin:8px 0;padding:6px 12px}
summary{cursor:pointer;display:flex;gap:12px;align-items:baseline;flex-wrap:wrap}summary b{font-size:15px}.mut{color:var(--mut)}
.rule{font-family:ui-monospace,monospace;background:var(--bg);border:1px solid var(--bd);border-radius:6px;padding:3px 8px;margin:4px 0;word-break:break-word;font-size:12px}
ul{list-style:none;padding:0;margin:6px 0}li{margin:3px 0;word-break:break-all;display:flex;gap:8px;align-items:baseline}
li.bad a{color:var(--bad);text-decoration:line-through}mark{background:var(--mark);color:inherit;border-radius:2px}a{color:inherit}
label{white-space:nowrap;font-size:12px;color:var(--mut)}.pc{color:var(--ok)}.pc.x{color:var(--bad)}.warn{color:var(--bad);font-size:12px}
</style>
<header><h1>Ocena reguł na estymatorze</h1>
<div class="stats" id="stats"></div>
<div class="bar"><button data-f="all" class="on">wszystkie</button><button data-f="err">ze złymi</button>
<button data-f="clean">bez złych</button><button data-f="todo">nieprzejrzane</button>
<input type="text" id="q" placeholder="szukaj marki lub URL-a">
<button id="bmd">statystyki (Markdown)</button><button id="bdl">pobierz oceny</button><button id="bld">wczytaj oceny</button><button id="brs">wyczyść</button>
<input type="file" id="fl" accept=".json" hidden></div>
<textarea id="md" hidden readonly></textarea></header>
<main id="main"></main>
<script>
const D=__DATA__, META=__META__, KEY='eval:'+META.id;
let bad=new Set(), rev=new Set(), flt='all';
try{const s=JSON.parse(localStorage.getItem(KEY)||'{}');bad=new Set(s.bad||[]);rev=new Set(s.rev||[])}catch(e){}
const save=()=>{try{localStorage.setItem(KEY,JSON.stringify({bad:[...bad],rev:[...rev]}))}catch(e){}};
const esc=s=>s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
const k=(b,u)=>b.id+'|'+u;
function hl(u,sp){let o='',i=0;for(const [a,b] of sp){if(a<i)continue;o+=esc(u.slice(i,a))+'<mark>'+esc(u.slice(a,b))+'</mark>';i=b}return o+esc(u.slice(i))}
function brandBad(b){return b.urls.filter(x=>bad.has(k(b,x.u))).length}
function stats(){
  let urls=0,badN=0,clean=0,rv=0,errs=[];
  for(const b of D){const n=b.urls.length,m=brandBad(b);urls+=n;badN+=m;if(m==0)clean++;else errs.push(b.id+' '+m+'/'+n);if(rev.has(b.id))rv++}
  return {brands:D.length,rules:META.rules,urls,bad:badN,prec:urls?(urls-badN)/urls:1,clean,rv,errs}
}
const pct=x=>(x*100).toFixed(1).replace('.',',')+'%';
function renderStats(){
  const s=stats();
  document.getElementById('stats').innerHTML=
   `<div class="stat big"><b>${pct(s.prec)}</b><span>precyzja (${s.urls-s.bad}/${s.urls} URL-i)</span></div>`+
   `<div class="stat"><b>${s.bad}</b><span>zaznaczone złe</span></div>`+
   `<div class="stat"><b>${s.clean}/${s.brands}</b><span>marek bez złych</span></div>`+
   `<div class="stat"><b>${s.rv}/${s.brands}</b><span>marek przejrzanych</span></div>`+
   `<div class="stat"><b>${s.rules}</b><span>reguł</span></div>`;
  return s;
}
function markdown(){
  const s=stats();
  return `### Ręczna ocena reguł na estymatorze (${META.when})\n\n`+
   `- reguł: ${s.rules}, marek: ${s.brands}\n- URL-i zwróconych przez estymator: ${s.urls}\n`+
   `- oznaczone ręcznie jako złe: ${s.bad}\n- **precyzja: ${pct(s.prec)}** (${s.urls-s.bad}/${s.urls})\n`+
   `- marek bez żadnego złego URL-a: ${s.clean}/${s.brands}\n- ręcznie przejrzano: ${s.rv}/${s.brands} marek\n`+
   (s.errs.length?`- marki ze złymi URL-ami: ${s.errs.join(', ')}\n`:'');
}
function card(b){
  const m=brandBad(b),n=b.urls.length;
  const li=b.urls.map(x=>`<li class="${bad.has(k(b,x.u))?'bad':''}"><label><input type="checkbox" data-u="${esc(x.u)}" ${bad.has(k(b,x.u))?'checked':''}> zły</label><a href="${esc(x.u)}" target="_blank" rel="noopener">${hl(x.u,x.s)}</a></li>`).join('');
  return `<details data-id="${esc(b.id)}"><summary><b>${esc(b.id)}</b><span class="mut">${n} URL-i</span>`+
   `<span class="pc ${m?'x':''}">${n?pct((n-m)/n):'-'}${m?' ('+m+' złych)':''}</span>`+
   `<label><input type="checkbox" data-rev ${rev.has(b.id)?'checked':''}> przejrzane</label></summary>`+
   b.rules.map(r=>`<div class="rule">${esc(r)}</div>`).join('')+
   (b.partial?'<div class="warn">uwaga: estymator zwrócił tylko część trafień</div>':'')+
   b.errors.map(e=>`<div class="warn">błąd: ${esc(e)}</div>`).join('')+`<ul>${li}</ul></details>`;
}
function visible(b){
  const q=document.getElementById('q').value.toLowerCase(),m=brandBad(b);
  if(q&&!(b.id.toLowerCase().includes(q)||b.urls.some(x=>x.u.toLowerCase().includes(q))))return false;
  return flt=='all'||(flt=='err'&&m>0)||(flt=='clean'&&m==0)||(flt=='todo'&&!rev.has(b.id));
}
function render(){
  const open=new Set([...document.querySelectorAll('details[open]')].map(d=>d.dataset.id));
  document.getElementById('main').innerHTML=D.filter(visible).map(card).join('');
  document.querySelectorAll('details').forEach(d=>{if(open.has(d.dataset.id))d.open=true});
  renderStats();
}
document.getElementById('main').addEventListener('change',e=>{
  const d=e.target.closest('details'),b=D.find(x=>x.id==d.dataset.id);
  if(e.target.dataset.rev!==undefined){e.target.checked?rev.add(b.id):rev.delete(b.id)}
  else{const key=k(b,e.target.dataset.u);e.target.checked?bad.add(key):bad.delete(key);if(e.target.checked)rev.add(b.id)}
  save();
  if(flt=='all'&&!document.getElementById('q').value){        // bez przebudowy listy: tylko odśwież kartę i statystyki
    const open=d.open,tmp=document.createElement('div');tmp.innerHTML=card(b);const nd=tmp.firstChild;nd.open=open;d.replaceWith(nd);renderStats();
  }else render();
});
document.querySelectorAll('[data-f]').forEach(x=>x.onclick=()=>{flt=x.dataset.f;document.querySelectorAll('[data-f]').forEach(y=>y.classList.toggle('on',y==x));render()});
document.getElementById('q').oninput=render;
document.getElementById('bmd').onclick=()=>{const t=document.getElementById('md');t.hidden=!t.hidden;t.value=markdown();t.select();try{navigator.clipboard.writeText(t.value)}catch(e){}};
document.getElementById('bdl').onclick=()=>{const a=document.createElement('a');a.href=URL.createObjectURL(new Blob([JSON.stringify({bad:[...bad],rev:[...rev]})],{type:'application/json'}));a.download='oceny.json';a.click()};
document.getElementById('bld').onclick=()=>document.getElementById('fl').click();
document.getElementById('fl').onchange=e=>{const f=e.target.files[0];if(!f)return;f.text().then(t=>{const s=JSON.parse(t);bad=new Set(s.bad||[]);rev=new Set(s.rev||[]);save();render()})};
document.getElementById('brs').onclick=()=>{if(confirm('Wyczyścić wszystkie zaznaczenia?')){bad.clear();rev.clear();save();render()}};
render();
</script>
"""

if __name__ == "__main__":
    main()