#!/usr/bin/env python3
"""Self-contained version of the software-mirror pipeline, meant to run inside
this repo's own GitHub Actions workflow (see .github/workflows/sync.yml).

Regenerates index.html + assets/icons/ at the repo root from Micropeptide's
public GitHub repos. Source of truth / dev copy of this pipeline lives at
/Users/runtianwu/Rdirectory/WebDevelop/sites/runtian-uk/software-mirror/ —
keep both in sync when changing the logic.
"""
import base64
import json
import re
import struct
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

GITHUB_USER = "Micropeptide"
EXCLUDE_REPOS = {"hello-runtian-uk", "software-runtian-uk", "about-runtian-uk"}
CUSTOM_DOMAIN = "software.runtian.uk"

REPO_ROOT = Path(__file__).resolve().parent.parent
ICON_DIR = REPO_ROOT / "assets" / "icons"

ICON_DIRS_TO_SCAN = [
    "Resources", "resources", "Resources/icon-source", "resources/icon-source",
    "assets", "Assets", "extension/images", "extension/icons", "images", "icons",
]
ICON_NAME_HINTS = ["icon", "logo", "appicon"]
LANG_COLORS = {"Swift": "#f05138", "Java": "#b07219", "HTML": "#e34c26", "Python": "#3572a5", "JavaScript": "#f1e05a"}
PLACEHOLDER_COLORS = ["#6366f1", "#0891b2", "#ea580c", "#65a30d", "#db2777"]


# ---------- GitHub data fetching ----------

def gh_api(path, accept_404=False):
    result = subprocess.run(["gh", "api", path], capture_output=True, text=True)
    if result.returncode != 0:
        if accept_404 and "404" in result.stderr:
            return None
        raise RuntimeError(f"gh api {path} failed: {result.stderr.strip()}")
    return json.loads(result.stdout)


def list_repos():
    result = subprocess.run(
        ["gh", "repo", "list", GITHUB_USER, "--limit", "100", "--source", "--json",
         "name,description,url,isFork,isArchived,primaryLanguage,licenseInfo,updatedAt"],
        capture_output=True, text=True, check=True,
    )
    repos = json.loads(result.stdout)
    return [r for r in repos if not r["isFork"] and not r["isArchived"] and r["name"] not in EXCLUDE_REPOS]


def png_dimensions(data):
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    return struct.unpack(">II", data[16:24])


def download(url):
    with urllib.request.urlopen(url) as resp:
        return resp.read()


def find_icon_download_url(repo_name):
    for d in ICON_DIRS_TO_SCAN:
        listing = gh_api(f"repos/{GITHUB_USER}/{repo_name}/contents/{d}", accept_404=True)
        if not listing:
            continue
        pngs = [f for f in listing if f["type"] == "file" and f["name"].lower().endswith(".png")]
        hinted = [f for f in pngs if any(h in f["name"].lower() for h in ICON_NAME_HINTS)]
        chosen = hinted or pngs
        if not chosen:
            continue
        if len(chosen) == 1:
            return chosen[0]["download_url"]
        best_url, best_area = None, -1
        for f in chosen:
            try:
                dims = png_dimensions(download(f["download_url"]))
            except Exception:
                dims = None
            if dims and dims[0] == dims[1] and dims[0] * dims[1] > best_area:
                best_url, best_area = f["download_url"], dims[0] * dims[1]
        return best_url or chosen[0]["download_url"]
    return None


def get_latest_release_dmg(repo_name):
    result = subprocess.run(
        ["gh", "release", "view", "-R", f"{GITHUB_USER}/{repo_name}", "--json", "tagName,assets"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None, None
    data = json.loads(result.stdout)
    dmg = next((a for a in data["assets"] if a["name"].lower().endswith(".dmg")), None)
    return (dmg["url"] if dmg else None), data["tagName"]


def get_display_order(repo_name):
    """Repos can opt into an explicit display order by adding a `.order` file
    at their root containing a single integer. Lower sorts first; repos
    without one sort after, by most-recently-updated."""
    f = gh_api(f"repos/{GITHUB_USER}/{repo_name}/contents/.order", accept_404=True)
    if not f:
        return None
    try:
        return int(base64.b64decode(f["content"]).decode().strip())
    except (ValueError, KeyError):
        return None


def get_readme_text(repo_name):
    readme = gh_api(f"repos/{GITHUB_USER}/{repo_name}/readme", accept_404=True)
    if not readme:
        return ""
    return base64.b64decode(readme["content"]).decode("utf-8", errors="replace")


def clean_md(text):
    text = re.sub(r"\*\*(.*?)\*\*", r"\1", text)
    text = re.sub(r"\[(.*?)\]\((.*?)\)", r"\1", text)
    text = re.sub(r"`(.*?)`", r"\1", text)
    return text.strip()


def extract_feature_bullets(readme_text, max_items=8):
    lines = readme_text.splitlines()
    bullets, in_list = [], False
    for line in lines:
        stripped = line.strip()
        if re.match(r"^#{1,6}\s", stripped):
            if bullets:
                break
            continue
        m = re.match(r"^[-*]\s+(.*)", stripped)
        if m:
            bullets.append(clean_md(m.group(1)))
            in_list = True
            continue
        if stripped == "":
            if in_list and len(bullets) >= 2:
                break
            continue
        if in_list and bullets:
            bullets[-1] = (bullets[-1] + " " + clean_md(stripped)).strip()
    return [b for b in bullets if len(b) > 3][:max_items]


def fetch_manifest():
    apps = []
    for r in list_repos():
        name = r["name"]
        print(f"Processing {name}...", file=sys.stderr)
        readme = get_readme_text(name)
        icon_url = find_icon_download_url(name)
        dmg_url, version = get_latest_release_dmg(name)
        apps.append({
            "name": name,
            "description": r["description"] or "",
            "url": r["url"],
            "releases_url": r["url"] + "/releases",
            "dmg_url": dmg_url,
            "version": version,
            "language": (r["primaryLanguage"] or {}).get("name"),
            "license": (r["licenseInfo"] or {}).get("name"),
            "updated_at": r["updatedAt"],
            "icon_download_url": icon_url,
            "features": extract_feature_bullets(readme),
            "order": get_display_order(name),
        })
    # Explicit .order wins (ascending); everything else follows, most-recently-updated
    # first (two stable sorts: recency breaks ties among the unordered apps).
    apps.sort(key=lambda a: a["updated_at"], reverse=True)
    apps.sort(key=lambda a: (a["order"] is None, a["order"] or 0))
    return apps


# ---------- Icon handling ----------

def placeholder_icon_svg(name):
    letter = name[0].upper()
    color = PLACEHOLDER_COLORS[sum(map(ord, name)) % len(PLACEHOLDER_COLORS)]
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="160" height="160">
  <rect width="160" height="160" rx="36" fill="{color}"/>
  <text x="80" y="80" font-family="-apple-system,sans-serif" font-size="72" font-weight="700"
        fill="#fff" text-anchor="middle" dominant-baseline="central">{letter}</text>
</svg>'''


def resize_png(data, size=160):
    from PIL import Image
    import io
    img = Image.open(io.BytesIO(data)).convert("RGBA")
    img = img.resize((size, size), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def materialize_icon(app):
    ICON_DIR.mkdir(parents=True, exist_ok=True)
    if app["icon_download_url"]:
        try:
            raw = download(app["icon_download_url"])
            resized = resize_png(raw, 160)
            (ICON_DIR / f"{app['name']}.png").write_bytes(resized)
            app["icon_ext"] = "png"
            return
        except Exception as e:
            print(f"icon fetch/resize failed for {app['name']}: {e}", file=sys.stderr)
    (ICON_DIR / f"{app['name']}.svg").write_text(placeholder_icon_svg(app["name"]))
    app["icon_ext"] = "svg"


# ---------- HTML rendering ----------

def slugify(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def badge(text, color="#586069", bg="#f1f3f5"):
    return f'<span class="badge" style="color:{color};background:{bg}">{text}</span>'


def render_nav(apps):
    items = "".join(f'<li><a href="#{slugify(a["name"])}">{a["name"]}</a></li>' for a in apps)
    return f'<nav class="nav-pane"><span class="nav-label">On this page</span><ul>{items}</ul></nav>'


def render_card(app):
    icon_file = f"assets/icons/{app['name']}.{app['icon_ext']}"
    slug = slugify(app["name"])
    lang = app.get("language")
    lang_color = LANG_COLORS.get(lang, "#586069")
    features_html = "".join(f"<li>{f}</li>" for f in app["features"][:4])
    updated = datetime.fromisoformat(app["updated_at"].replace("Z", "+00:00"))
    updated_str = updated.strftime("%b %Y")
    return f"""
      <article class="card" id="{slug}">
        <div class="card-head">
          <img class="icon" src="{icon_file}" alt="{app['name']} icon" width="64" height="64" loading="lazy">
          <div class="card-title">
            <h2>{app['name']}</h2>
            <div class="badges">
              {badge(lang, "#fff", lang_color) if lang else ""}
              {badge(app['license']) if app.get('license') else ""}
            </div>
          </div>
        </div>
        <p class="desc">{app['description']}</p>
        {"<ul class='features'>" + features_html + "</ul>" if features_html else ""}
        <div class="card-foot">
          <span class="updated">{app['version'] + ' &middot; ' if app.get('version') else ''}Updated {updated_str}</span>
          <div class="actions">
            <a class="btn btn-outline" href="{app['url']}" target="_blank" rel="noopener">Source</a>
            <a class="btn btn-outline" href="{app['releases_url']}" target="_blank" rel="noopener">Releases</a>
            {f'<a class="btn btn-primary" href="{app["dmg_url"]}" download>Download</a>' if app.get('dmg_url') else ''}
          </div>
        </div>
      </article>"""


PAGE_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Software — Runtian Wu</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="description" content="Software built by Runtian Wu — macOS utilities and research tools.">
<style>
  :root {{
    --bg: #ffffff; --bg-subtle: #f8f9fa; --text: #1a1a1a; --text-muted: #6b7280;
    --border: #e5e7eb; --primary: #2b2b2b; --primary-contrast: #ffffff;
    --shadow: 0 1px 2px rgba(0,0,0,0.04), 0 1px 8px rgba(0,0,0,0.04);
    --shadow-hover: 0 4px 10px rgba(0,0,0,0.06), 0 8px 24px rgba(0,0,0,0.08);
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: var(--bg); color: var(--text); line-height: 1.5; }}
  a {{ color: inherit; }}
  .wrap {{ max-width: 1180px; margin: 0 auto; padding: 0 1.5rem 4rem; }}
  header.site {{ padding: 2rem 0 0.5rem; }}
  .back-link {{ display: inline-block; font-size: 0.875rem; color: var(--text-muted); text-decoration: none; margin-bottom: 1.5rem; }}
  .back-link:hover {{ color: var(--text); }}
  h1 {{ font-size: 2.5rem; margin: 0 0 0.5rem; letter-spacing: -0.02em; }}
  .intro {{ color: var(--text-muted); max-width: 620px; margin: 0 0 2.5rem; font-size: 1.05rem; }}
  .layout {{ display: flex; gap: 2.5rem; align-items: flex-start; }}
  .nav-pane {{ flex: 0 0 200px; position: sticky; top: 1.5rem; max-height: calc(100vh - 3rem); overflow-y: auto; }}
  .nav-label {{ display: block; font-size: 0.75rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.04em; color: var(--text-muted); margin-bottom: 0.75rem; }}
  .nav-pane ul {{ list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 0.6rem; }}
  .nav-pane a {{ text-decoration: none; color: var(--text-muted); font-size: 0.9rem; border-left: 2px solid var(--border); padding-left: 0.75rem; display: block; transition: color 0.15s ease, border-color 0.15s ease; }}
  .nav-pane a:hover {{ color: var(--text); border-color: var(--text); }}
  .nav-pane a.active {{ color: var(--text); border-color: var(--text); font-weight: 600; }}
  .content {{ flex: 1; min-width: 0; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 1.25rem; }}
  .card {{ scroll-margin-top: 1.5rem; background: var(--bg); border: 1px solid var(--border); border-radius: 16px; padding: 1.5rem; box-shadow: var(--shadow); transition: transform 0.15s ease, box-shadow 0.15s ease, border-color 0.15s ease; display: flex; flex-direction: column; }}
  .card:hover {{ transform: translateY(-3px); box-shadow: var(--shadow-hover); border-color: transparent; }}
  @media (max-width: 700px) {{
    .layout {{ flex-direction: column; }}
    .nav-pane {{ position: static; max-height: none; flex: 1 1 auto; width: 100%; }}
    .nav-pane ul {{ flex-direction: row; flex-wrap: wrap; }}
  }}
  .card-head {{ display: flex; gap: 1rem; align-items: center; margin-bottom: 1rem; }}
  .icon {{ width: 64px; height: 64px; border-radius: 14px; flex-shrink: 0; box-shadow: 0 1px 4px rgba(0,0,0,0.15); }}
  .card-title h2 {{ font-size: 1.25rem; margin: 0 0 0.35rem; }}
  .badges {{ display: flex; gap: 0.4rem; flex-wrap: wrap; }}
  .badge {{ font-size: 0.7rem; font-weight: 600; padding: 0.15rem 0.55rem; border-radius: 999px; text-transform: uppercase; letter-spacing: 0.02em; }}
  .desc {{ color: var(--text-muted); font-size: 0.925rem; margin: 0 0 1rem; }}
  .features {{ list-style: none; padding: 0; margin: 0 0 1.25rem; font-size: 0.875rem; color: var(--text); display: flex; flex-direction: column; gap: 0.4rem; flex-grow: 1; }}
  .features li {{ padding-left: 1.3rem; position: relative; }}
  .features li::before {{ content: "✓"; position: absolute; left: 0; color: #16a34a; font-weight: 700; }}
  .card-foot {{ margin-top: auto; padding-top: 1rem; border-top: 1px solid var(--border); display: flex; flex-direction: column; gap: 0.75rem; }}
  .updated {{ font-size: 0.75rem; color: var(--text-muted); white-space: nowrap; }}
  .actions {{ display: flex; gap: 0.5rem; flex-wrap: wrap; }}
  .actions .btn {{ flex: 1 1 auto; text-align: center; }}
  .btn {{ font-size: 0.8125rem; font-weight: 600; padding: 0.4rem 0.85rem; border-radius: 8px; text-decoration: none; transition: opacity 0.15s ease; }}
  .btn:hover {{ opacity: 0.8; }}
  .btn-primary {{ background: var(--primary); color: var(--primary-contrast); }}
  .btn-outline {{ border: 1px solid var(--border); color: var(--text); }}
  footer.site {{ margin-top: 3rem; padding-top: 1.5rem; border-top: 1px solid var(--border); font-size: 0.8rem; color: var(--text-muted); }}
</style>
</head>
<body>
<div class="wrap">
  <header class="site">
    <a class="back-link" href="https://runtian.uk">&larr; runtian.uk</a>
    <h1>Software</h1>
    <p class="intro">This is a collection of tools I&rsquo;ve made for research, productivity, automation, and the occasional oddly specific problem. Most started with a simple thought: surely there&rsquo;s a better way to do this.</p>
  </header>
  <div class="layout">
    {nav}
    <main class="content">
      <div class="grid">
{cards}
      </div>
    </main>
  </div>
  <footer class="site">
    Last synced {synced_at} &middot; generated from public GitHub repository data
  </footer>
</div>
<script>
  (function() {{
    var links = Array.from(document.querySelectorAll('.nav-pane a'));
    var sections = links.map(function(l) {{ return document.getElementById(l.getAttribute('href').slice(1)); }});
    function onScroll() {{
      var pos = window.scrollY + 100;
      var activeIdx = 0;
      sections.forEach(function(sec, i) {{
        if (sec && sec.offsetTop <= pos) activeIdx = i;
      }});
      links.forEach(function(l, i) {{ l.classList.toggle('active', i === activeIdx); }});
    }}
    document.addEventListener('scroll', onScroll, {{ passive: true }});
    onScroll();
  }})();
</script>
</body>
</html>
"""


def main():
    apps = fetch_manifest()
    for app in apps:
        materialize_icon(app)

    # Remove icons for apps no longer in the list (renamed/deleted repos).
    keep = {f"{a['name']}.{a['icon_ext']}" for a in apps}
    if ICON_DIR.exists():
        for f in ICON_DIR.iterdir():
            if f.name not in keep:
                f.unlink()

    cards_html = "\n".join(render_card(a) for a in apps)
    nav_html = render_nav(apps)
    synced_at = datetime.now(timezone.utc).strftime("%B %d, %Y")
    page = PAGE_TEMPLATE.format(cards=cards_html, nav=nav_html, synced_at=synced_at)
    (REPO_ROOT / "index.html").write_text(page)
    print(f"Synced {len(apps)} apps into {REPO_ROOT / 'index.html'}")


if __name__ == "__main__":
    main()
