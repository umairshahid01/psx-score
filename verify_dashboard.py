"""Verify the rebuilt dashboard: DOM contract, CSS coverage, structure."""
import io, json, re, sys

P = sys.argv[1] if len(sys.argv) > 1 else "/home/claude/build/dashboard_v5.html"
src = io.open(P, encoding="utf-8").read()

si, sj = src.index("<style>"), src.rindex("</style>")
css = src[si+7:sj]
bi = src.index("<body")
ki, kj = src.rindex("<script>"), src.rindex("</script>")
markup_all = src[bi:ki]
js = src[ki+8:kj]
markup = re.sub(r'<script id="demo-bundle".*?</script>',
                '<script id="demo-bundle"></script>', markup_all, flags=re.S)

FAIL, WARN = [], []
def fail(m): FAIL.append(m); print("  [FAIL]", m)
def warn(m): WARN.append(m); print("  [WARN]", m)
def ok(m):   print("  [ OK ]", m)

print("\n1. Structure")
for tag in ("<style>", "</style>", "<body", "</body>", "</html>"):
    (ok if tag in src else fail)(f"{tag} present")
ok(f"css {css.count(chr(10))} lines · markup {markup.count(chr(10))} lines · "
   f"js {js.count(chr(10))} lines")

print("\n2. Tag balance (markup)")
for t in ("div", "section", "button", "svg"):
    o = len(re.findall(r"<%s[\s>]" % t, markup))
    c = len(re.findall(r"</%s>" % t, markup))
    if t == "svg" or o == c:
        (ok if o == c else warn)(f"<{t}> {o} open / {c} close")
    else:
        fail(f"<{t}> UNBALANCED: {o} open / {c} close")

print("\n3. DOM contract — every id the JS asks for")
ids_js = set()
for pat in (r"\$\(\s*'([A-Za-z0-9_\-]+)'\s*\)",
            r"getElementById\(\s*'([A-Za-z0-9_\-]+)'\s*\)",
            r'getElementById\(\s*"([A-Za-z0-9_\-]+)"\s*\)'):
    ids_js |= set(re.findall(pat, js))
ids_markup = set(re.findall(r'\sid="([A-Za-z0-9_\-]+)"', markup))
created = set(re.findall(r"\.id\s*=\s*'([A-Za-z0-9_\-]+)'", js))
created |= set(re.findall(r'id="([A-Za-z0-9_\-]+)"', js))
# ids built from template expressions, e.g. id="${hubId}" in buildGauge()
created |= {'gauge-hub', 'gauge-arc', 'gauge-needle'}
missing = sorted(ids_js - ids_markup - created)
if missing:
    fail(f"ids referenced but absent: {missing}")
else:
    ok(f"all {len(ids_js)} referenced ids resolve "
       f"({len(ids_markup)} in markup, {len(created)} built at runtime)")

# ids reached through variable concatenation
for base in ("pv-tech", "pv-fund", "pv-comb"):
    for suf in ("", "-bar"):
        n = base + suf
        (ok if n in ids_markup else fail)(f"dynamic id {n}")

print("\n4. Selectors the JS queries")
sels = set()
for pat in (r"querySelector(?:All)?\(\s*'([^']+)'\s*\)",
            r"\.closest\(\s*'([^']+)'\s*\)"):
    sels |= set(re.findall(pat, js))
js_flat = re.sub(r"\$\{[^{}]*\}", " ", js)
gen = set(re.findall(r'class="([^"<>]+)"', js_flat))
gen_classes = set()
for g in gen:
    gen_classes |= {c for c in g.split() if re.fullmatch(r"[a-zA-Z][\w\-]*", c)}
markup_classes = set()
for g in re.findall(r'class="([^"<>{}]+)"', markup):
    markup_classes |= {c for c in g.split() if re.fullmatch(r"[a-zA-Z][\w\-]*", c)}
unresolved = []
for s in sorted(sels):
    if s in ("div", "i", "defs"):
        continue
    base = s.split()[-1].split(":")[0]
    m = re.match(r"^[.#]?([A-Za-z0-9_\-]+)", base)
    if not m:
        continue
    name = m.group(1)
    if base.startswith("#"):
        if name not in ids_markup and name not in created:
            unresolved.append(s)
    elif base.startswith("."):
        if name not in markup_classes and name not in gen_classes:
            unresolved.append(s)
if unresolved:
    warn(f"selectors with no matching element anywhere: {unresolved}")
else:
    ok(f"all {len(sels)} JS selectors have a target in markup or generated HTML")

print("\n5. CSS coverage of every class used")
all_classes = markup_classes | gen_classes
css_classes = set(re.findall(r"\.([A-Za-z][\w\-]*)", css))
uncovered = sorted(c for c in all_classes if c not in css_classes)
# purely-behavioural markers need no styling
behavioural = {"on","active","show","open","live","demo","hide","gone","merged",
               "bub-hide","ready","dragging","expanded","fade","warn","is-na",
               "v5-in","v5-pending","v5-stagger","reveal","view-in","booting",
               "b","em","g","k","v","t","big","dot","ic","lead","nm","sym","sec",
               "now","px","buy","stop","tgt","zone","cd","ck","rv","pk","lbl",
               "chip","tag","metric","empty","opt"}
real = [c for c in uncovered if c not in behavioural]
if real:
    warn(f"{len(real)} class(es) used but never styled: {real[:24]}")
else:
    ok(f"all {len(all_classes)} classes used are styled (or purely stateful)")

print("\n6. Leftovers from the old design")
legacy = ["boot-puzzle", "boot-pile", "boot-coin", "bp-piece", "jigsaw",
          "topbar", "home-hero", "depth-cta-wrap", "search-console",
          "score-of'", "load-candles", "etf-card", "modal-card", "exp-bar"]
found = [l for l in legacy if l in src]
if found:
    warn(f"old-design references still present: {found}")
else:
    ok("no references to the retired v4 layout remain")

print("\n7. CSS sanity")
(ok if css.count("{") == css.count("}") else fail)(
    f"braces balanced ({css.count('{')} open / {css.count('}')} close)")
(ok if "@media (prefers-reduced-motion:reduce)" in css else fail)(
    "reduced-motion block present")
for tok in ("--bull", "--spring", "--surface", "--mono"):
    (ok if tok in css else fail)(f"token {tok} defined")

print("\n" + "=" * 62)
print(f"  {len(FAIL)} failures, {len(WARN)} warnings")
print("=" * 62)
sys.exit(1 if FAIL else 0)
