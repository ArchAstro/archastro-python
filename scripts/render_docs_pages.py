from __future__ import annotations

from html import escape
from pathlib import Path

import markdown2


ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
SITE = ROOT / "site"

PAGES = [
    ("index.md", "index.html", "Overview"),
    ("authentication.md", "authentication.html", "Authentication"),
    ("scenarios.md", "scenarios.html", "Scenarios"),
]


def main() -> None:
    SITE.mkdir(exist_ok=True)
    for source_name, output_name, title in PAGES:
        markdown = (DOCS / source_name).read_text(encoding="utf-8")
        body = markdown2.markdown(
            markdown,
            extras=["fenced-code-blocks", "header-ids", "tables"],
        )
        (SITE / output_name).write_text(render_page(title, body), encoding="utf-8")


def render_page(title: str, body: str) -> str:
    nav_items = "\n".join(
        f'<li><a href="{escape(output)}">{escape(label)}</a></li>'
        for _, output, label in PAGES
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)} - ArchAstro Python SDK</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #ffffff;
      --fg: #1f2933;
      --muted: #5c6f82;
      --line: #d8dee5;
      --panel: #f7f9fb;
      --accent: #176c5f;
      --code: #0f1720;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      color: var(--fg);
      background: var(--bg);
      font: 16px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    .layout {{
      display: grid;
      grid-template-columns: minmax(220px, 280px) minmax(0, 880px);
      min-height: 100vh;
    }}
    nav {{
      border-right: 1px solid var(--line);
      background: var(--panel);
      padding: 28px 24px;
    }}
    nav h2 {{ margin: 0 0 16px; font-size: 18px; }}
    nav ul {{ list-style: none; margin: 0 0 28px; padding: 0; }}
    nav li {{ margin: 10px 0; }}
    nav a {{ color: var(--fg); text-decoration: none; }}
    nav a:hover {{ color: var(--accent); }}
    main {{ padding: 44px 56px 72px; }}
    h1 {{ margin-top: 0; font-size: 40px; line-height: 1.1; }}
    h2 {{ margin-top: 40px; border-top: 1px solid var(--line); padding-top: 28px; }}
    a {{ color: var(--accent); }}
    p, li {{ color: var(--fg); }}
    code {{
      color: var(--code);
      background: #eef2f5;
      border-radius: 4px;
      padding: 0.1em 0.3em;
    }}
    pre {{
      overflow-x: auto;
      background: #101820;
      color: #f8fafc;
      border-radius: 6px;
      padding: 18px;
    }}
    pre code {{ background: transparent; color: inherit; padding: 0; }}
    @media (max-width: 760px) {{
      .layout {{ display: block; }}
      nav {{ border-right: 0; border-bottom: 1px solid var(--line); }}
      main {{ padding: 32px 24px 56px; }}
      h1 {{ font-size: 32px; }}
    }}
  </style>
</head>
<body>
  <div class="layout">
    <nav>
      <h2>Guides</h2>
      <ul>{nav_items}</ul>
      <h2>API Reference</h2>
      <ul>
        <li><a href="archastro/platform.html">Platform API</a></li>
        <li><a href="archastro/phx_channel.html">Phoenix Channels</a></li>
      </ul>
    </nav>
    <main>{body}</main>
  </div>
</body>
</html>
"""


if __name__ == "__main__":
    main()
