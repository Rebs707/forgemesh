#!/bin/sh
# Inject custom CSS into Grafana's HTML template before startup
INDEX=/usr/share/grafana/public/views/index.html

python3 - <<'PYEOF'
import re

css = """    <style>
      /* Smaller Grafana logs panel text */
      [data-testid="data-testid Log row"],
      [data-testid="data-testid Log row"] * {
        font-size: 10px !important;
        line-height: 1.4 !important;
      }
    </style>"""

with open("/usr/share/grafana/public/views/index.html", "r") as f:
    html = f.read()

if "<style>" not in html:
    html = html.replace("</head>", css + "\n  </head>", 1)
    with open("/usr/share/grafana/public/views/index.html", "w") as f:
        f.write(html)
PYEOF

exec /run.sh "$@"
