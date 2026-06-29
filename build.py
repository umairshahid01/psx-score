"""
build.py
========
Assemble the final desktop dashboard by embedding the demo bundle JSON into
the HTML template.

    dashboard_template.html  +  demo_bundle.json   ->   dashboard.html

Run this after editing dashboard_template.html:

    python build.py
"""
import json

with open("demo_bundle.json", "r", encoding="utf-8") as f:
    bundle = f.read().strip()

with open("dashboard_template.html", "r", encoding="utf-8") as f:
    template = f.read()

html = template.replace("__DEMO_BUNDLE_JSON__", bundle)

with open("dashboard.html", "w", encoding="utf-8") as f:
    f.write(html)

print(f"Built dashboard.html: {len(html):,} bytes")
