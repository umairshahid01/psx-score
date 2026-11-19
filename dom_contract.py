"""Extract the DOM contract that dashboard.html's JS depends on."""
import io
import json
import re
import sys

PATH = sys.argv[1] if len(sys.argv) > 1 else "/home/claude/psx_src/dashboard.html"
src = io.open(PATH, encoding="utf-8").read()

# ---- split the document ---------------------------------------------------
style_i = src.index("<style>")
style_j = src.rindex("</style>")
css = src[style_i + 7:style_j]

body_i = src.index("<body")
script_i = src.index("<script>", body_i)
script_j = src.rindex("</script>")
markup = src[body_i:script_i]
js = src[script_i + 8:script_j]

# the embedded demo bundle lives in markup as a <script type=application/json>
markup_wo_bundle = re.sub(
    r'<script id="demo-bundle".*?</script>', "", markup, flags=re.S)

# ---- ids the JS asks for --------------------------------------------------
ids = set()
for pat in (r"\$\(\s*'([A-Za-z0-9_\-]+)'\s*\)",
            r'\$\(\s*"([A-Za-z0-9_\-]+)"\s*\)',
            r"getElementById\(\s*'([A-Za-z0-9_\-]+)'\s*\)",
            r'getElementById\(\s*"([A-Za-z0-9_\-]+)"\s*\)'):
    ids |= set(re.findall(pat, js))

# ids built dynamically via string concat, e.g. $(id+'-bar') — record the suffixes
suffix_ids = set(re.findall(r"\$\(\s*\w+\s*\+\s*'(-[A-Za-z0-9_\-]+)'\s*\)", js))

# ---- css/query selectors the JS uses -------------------------------------
sel = set()
for pat in (r"querySelector(?:All)?\(\s*'([^']+)'\s*\)",
            r'querySelector(?:All)?\(\s*"([^"]+)"\s*\)',
            r"\.closest\(\s*'([^']+)'\s*\)"):
    sel |= set(re.findall(pat, js))

# ---- classes the JS adds/removes/toggles ---------------------------------
cls = set()
for pat in (r"classList\.(?:add|remove|toggle|contains)\(\s*'([^']+)'",
            r'classList\.(?:add|remove|toggle|contains)\(\s*"([^"]+)"'):
    for m in re.findall(pat, js):
        cls.add(m)

# ---- ids present in the markup -------------------------------------------
present = set(re.findall(r'\sid="([A-Za-z0-9_\-]+)"', markup_wo_bundle))
# ids the JS itself creates at runtime
created = set(re.findall(r"\.id\s*=\s*'([A-Za-z0-9_\-]+)'", js))
created |= set(re.findall(r'id="([A-Za-z0-9_\-]+)"', js))

missing = sorted(i for i in ids if i not in present and i not in created)

out = {
    "required_ids": sorted(ids),
    "ids_in_markup": sorted(present),
    "ids_created_by_js": sorted(created),
    "dynamic_id_suffixes": sorted(suffix_ids),
    "selectors": sorted(sel),
    "classes_toggled": sorted(cls),
    "missing_now": missing,
    "counts": {
        "required_ids": len(ids), "in_markup": len(present),
        "created_by_js": len(created), "selectors": len(sel),
        "classes": len(cls), "markup_lines": markup.count("\n"),
        "css_lines": css.count("\n"), "js_lines": js.count("\n"),
    },
}
io.open("/home/claude/dom_contract.json", "w", encoding="utf-8").write(
    json.dumps(out, indent=1))
print(json.dumps(out["counts"], indent=1))
print("\nmissing ids (JS asks, markup lacks):", missing or "none")
