"""
Build the final index.html by embedding the demo bundle JSON into the HTML template.
"""
import json

# Read demo bundle
with open('demo_bundle.json', 'r') as f:
    bundle = f.read().strip()

# Read template
with open('index_template.html', 'r') as f:
    template = f.read()

# Replace placeholder
html = template.replace('__DEMO_BUNDLE_JSON__', bundle)

with open('index.html', 'w') as f:
    f.write(html)

print(f"Built index.html: {len(html):,} bytes")
