#!/usr/bin/env python3
"""
scripts/build_branding.py

Deterministic brand and architecture asset compiler for StigmeRig.
Renders four stigmergic candidate marks to scratch storage and compiles
the official mark, contributor-neutral social preview, and control-plane
architecture diagram into assets/ with SHA-256 provenance in manifest.json.
"""

import argparse
import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from playwright.sync_api import sync_playwright

# ---------------------------------------------------------------------------
# CANDIDATE A: "Apiary Honeycomb Architecture"
# Geometric worker bee surveying an interlocking honeycomb matrix.
# ---------------------------------------------------------------------------
SVG_OPTION_A = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" width="100%" height="100%">
  <!-- Background: Deep Propolis Charcoal -->
  <rect width="512" height="512" rx="112" fill="#161412" />

  <!-- Outer Fine Hive Framing Ring -->
  <circle cx="256" cy="236" r="198" fill="none" stroke="#382e25" stroke-width="1.5" stroke-dasharray="3 6" opacity="0.6"/>
  <circle cx="256" cy="236" r="186" fill="none" stroke="#68553f" stroke-width="1" opacity="0.3"/>

  <!-- Honeycomb Matrix (Stigmergic Wax Cells) -->
  <g fill="none" stroke-width="2">
    <!-- Outer Inactive Cells (Faint) -->
    <g stroke="#3a3024" opacity="0.5">
      <polygon points="256,56 298,80 298,128 256,152 214,128 214,80" />
      <polygon points="340,104 382,128 382,176 340,200 298,176 298,128" />
      <polygon points="340,200 382,224 382,272 340,296 298,272 298,224" />
      <polygon points="256,344 298,368 298,416 256,440 214,416 214,368" />
      <polygon points="172,200 214,224 214,272 172,296 130,272 130,224" />
      <polygon points="172,104 214,128 214,176 172,200 130,176 130,128" />
    </g>

    <!-- Active Brood / Honey Comb Core (Amber Glow) -->
    <polygon points="256,152 298,176 298,224 256,248 214,224 214,176" stroke="#d97706" stroke-width="2.5" fill="#241c14" />
    <polygon points="256,248 298,272 298,320 256,344 214,320 214,272" stroke="#b45309" stroke-width="2" fill="#1e1811" />
  </g>

  <!-- Wax Deposition Points & Environmental Cues (Stigmergic Nodes) -->
  <g fill="#f59e0b">
    <circle cx="256" cy="152" r="3.5" />
    <circle cx="298" cy="176" r="3.5" />
    <circle cx="298" cy="224" r="3.5" />
    <circle cx="256" cy="248" r="4" fill="#fbbf24" />
    <circle cx="214" cy="224" r="3.5" />
    <circle cx="214" cy="176" r="3.5" />
    <circle cx="298" cy="272" r="3.5" />
    <circle cx="214" cy="272" r="3.5" />
    <circle cx="256" cy="344" r="3.5" />
  </g>

  <!-- Geometric Worker Bee (Coordinating Agent) -->
  <g id="worker-bee" transform="translate(256, 218)">
    <!-- Wings: Translucent Hexagonal Facets -->
    <polygon points="0,-16 -78,-72 -112,-48 -32,2" fill="#fef3c7" fill-opacity="0.22" stroke="#fbbf24" stroke-width="2" />
    <line x1="-32" y1="-30" x2="-86" y2="-56" stroke="#f59e0b" stroke-width="1.5" opacity="0.6"/>
    <polygon points="-8,-4 -84,-12 -68,22 -16,8" fill="#fef3c7" fill-opacity="0.16" stroke="#f59e0b" stroke-width="1.5" />

    <polygon points="0,-16 78,-72 112,-48 32,2" fill="#fef3c7" fill-opacity="0.22" stroke="#fbbf24" stroke-width="2" />
    <line x1="32" y1="-30" x2="86" y2="-56" stroke="#f59e0b" stroke-width="1.5" opacity="0.6"/>
    <polygon points="8,-4 84,-12 68,22 16,8" fill="#fef3c7" fill-opacity="0.16" stroke="#f59e0b" stroke-width="1.5" />

    <!-- Head & Antennae -->
    <circle cx="0" cy="-44" r="11" fill="#292017" stroke="#f59e0b" stroke-width="2" />
    <circle cx="-5" cy="-46" r="3" fill="#fbf7ee" />
    <circle cx="5" cy="-46" r="3" fill="#fbf7ee" />
    <path d="M -4,-54 Q -12,-72 -22,-74" fill="none" stroke="#f59e0b" stroke-width="2" stroke-linecap="round"/>
    <path d="M 4,-54 Q 12,-72 22,-74" fill="none" stroke="#f59e0b" stroke-width="2" stroke-linecap="round"/>

    <!-- Thorax -->
    <path d="M 0,-34 C 18,-34 22,-8 0,-4 C -22,-8 -18,-34 0,-34 Z" fill="#382a1d" stroke="#f59e0b" stroke-width="2.5" />
    <circle cx="0" cy="-19" r="6" fill="#f59e0b" />

    <!-- Abdomen Segments -->
    <path d="M -16,-2 C -18,12 18,12 16,-2 Z" fill="#f59e0b" stroke="#161412" stroke-width="1.5" />
    <path d="M -15,10 C -16,22 16,22 15,10 Z" fill="#241a12" stroke="#f59e0b" stroke-width="1" />
    <path d="M -13,20 C -14,32 14,32 13,20 Z" fill="#fbbf24" stroke="#161412" stroke-width="1.5" />
    <path d="M -10,30 C -10,40 10,40 10,30 Z" fill="#241a12" stroke="#f59e0b" stroke-width="1" />
    <polygon points="-7,38 7,38 0,56" fill="#f59e0b" stroke="#241a12" stroke-width="1" />
  </g>

  <!-- StigmeRig Wordmark -->
  <text x="256" y="470" font-family="'Space Grotesk', 'Plus Jakarta Sans', 'Segoe UI', system-ui, sans-serif" font-size="22" fill="#fbf7ee" letter-spacing="7" text-anchor="middle" font-weight="700">STIGMERIG</text>
</svg>"""

# ---------------------------------------------------------------------------
# CANDIDATE B: "Waggle Dance Vector Matrix"
# Figure-8 waggle dance vector path surrounded by honeycomb cells.
# ---------------------------------------------------------------------------
SVG_OPTION_B = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" width="100%" height="100%">
  <!-- Background: Deep Warm Espresso Slate -->
  <rect width="512" height="512" rx="112" fill="#141210" />

  <!-- Outer Dial: Sun Compass Reticle -->
  <circle cx="256" cy="236" r="192" fill="none" stroke="#2e261f" stroke-width="2" />
  <circle cx="256" cy="236" r="176" fill="none" stroke="#4a3b2c" stroke-width="1" stroke-dasharray="4 8" opacity="0.6"/>

  <!-- Sun Reference Line -->
  <line x1="256" y1="44" x2="256" y2="92" stroke="#fbbf24" stroke-width="2.5" stroke-linecap="round" />
  <polygon points="256,40 250,56 262,56" fill="#fbbf24" />
  <circle cx="256" cy="236" r="6" fill="#fbbf24" />

  <!-- Honeycomb Frame Arc -->
  <g fill="none" stroke="#3d3023" stroke-width="1.5" opacity="0.6">
    <polygon points="120,120 152,138 152,174 120,192 88,174 88,138" />
    <polygon points="120,280 152,298 152,334 120,352 88,334 88,298" />
    <polygon points="392,120 424,138 424,174 392,192 360,174 360,138" />
    <polygon points="392,280 424,298 424,334 392,352 360,334 360,298" />
  </g>

  <!-- Figure-8 Waggle Dance Path -->
  <path d="M 256,160 C 140,160 140,312 256,312" fill="none" stroke="#d97706" stroke-width="2.5" stroke-linecap="round" stroke-dasharray="5 5" opacity="0.75"/>
  <path d="M 256,312 C 372,312 372,160 256,160" fill="none" stroke="#d97706" stroke-width="2.5" stroke-linecap="round" stroke-dasharray="5 5" opacity="0.75"/>

  <!-- Waggle Run Center Corridor -->
  <path d="M 256,312 L 256,160" stroke="#f59e0b" stroke-width="4" stroke-linecap="round"/>
  <g stroke="#fbbf24" stroke-width="2" stroke-linecap="round">
    <line x1="244" y1="290" x2="268" y2="290" />
    <line x1="240" y1="270" x2="272" y2="270" />
    <line x1="236" y1="250" x2="276" y2="250" />
    <line x1="240" y1="230" x2="272" y2="230" />
    <line x1="244" y1="210" x2="268" y2="210" />
    <line x1="248" y1="190" x2="264" y2="190" />
  </g>

  <!-- Dancing Bee Silhouette -->
  <g transform="translate(256, 236)">
    <polygon points="0,-8 -54,-40 -40,4 0,0" fill="#fef3c7" fill-opacity="0.3" stroke="#fbbf24" stroke-width="1.5" />
    <polygon points="0,-8 54,-40 40,4 0,0" fill="#fef3c7" fill-opacity="0.3" stroke="#fbbf24" stroke-width="1.5" />
    <ellipse cx="0" cy="0" rx="9" ry="18" fill="#141210" stroke="#f59e0b" stroke-width="2.5" />
    <line x1="-7" y1="-4" x2="7" y2="-4" stroke="#fbbf24" stroke-width="2" />
    <line x1="-8" y1="4" x2="8" y2="4" stroke="#fbbf24" stroke-width="2" />
  </g>

  <!-- Observer Agent Nodes -->
  <g fill="#f59e0b" stroke="#141210" stroke-width="2">
    <circle cx="196" cy="200" r="7" />
    <circle cx="196" cy="272" r="7" />
    <circle cx="316" cy="200" r="7" />
    <circle cx="316" cy="272" r="7" />
  </g>

  <!-- Wordmark -->
  <text x="256" y="470" font-family="'Space Grotesk', 'Plus Jakarta Sans', 'Segoe UI', system-ui, sans-serif" font-size="22" fill="#fbf7ee" letter-spacing="7" text-anchor="middle" font-weight="700">STIGMERIG</text>
</svg>"""

# ---------------------------------------------------------------------------
# CANDIDATE C: "The Comb Guild"
# Three geometric bees coordinating around a shared central hexagonal cell.
# ---------------------------------------------------------------------------
SVG_OPTION_C = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" width="100%" height="100%">
  <!-- Background: Deep Umber Charcoal -->
  <rect width="512" height="512" rx="112" fill="#151311" />

  <!-- Outer Guild Seal -->
  <circle cx="256" cy="236" r="194" fill="none" stroke="#2c2219" stroke-width="2"/>
  <circle cx="256" cy="236" r="182" fill="none" stroke="#544130" stroke-width="1.5" stroke-dasharray="3 6"/>

  <!-- Central Shared Comb Cell -->
  <polygon points="256,180 304,208 304,264 256,292 208,264 208,208" fill="#241a12" stroke="#d97706" stroke-width="3"/>
  <circle cx="256" cy="236" r="8" fill="#fbbf24" stroke="#151311" stroke-width="2"/>

  <!-- Surrounding Honeycomb Facets -->
  <g fill="none" stroke="#3d2f22" stroke-width="1.5" opacity="0.7">
    <polygon points="256,68 304,96 304,152 256,180 208,152 208,96" />
    <polygon points="352,236 400,264 400,320 352,348 304,320 304,264" />
    <polygon points="160,236 208,264 208,320 160,348 112,320 112,264" />
  </g>

  <!-- Top Bee (Worker 1: Builder) -->
  <g transform="translate(256, 120)">
    <polygon points="0,0 -42,-28 -28,8 0,4" fill="#fef3c7" fill-opacity="0.25" stroke="#f59e0b" stroke-width="1.5"/>
    <polygon points="0,0 42,-28 28,8 0,4" fill="#fef3c7" fill-opacity="0.25" stroke="#f59e0b" stroke-width="1.5"/>
    <path d="M 0,-18 C 10,-18 12,14 0,22 C -12,14 -10,-18 0,-18 Z" fill="#241a12" stroke="#fbbf24" stroke-width="2"/>
    <line x1="-7" y1="-4" x2="7" y2="-4" stroke="#f59e0b" stroke-width="2"/>
    <line x1="-8" y1="4" x2="8" y2="4" stroke="#fbbf24" stroke-width="2"/>
    <line x1="-6" y1="12" x2="6" y2="12" stroke="#f59e0b" stroke-width="2"/>
  </g>

  <!-- Bottom-Right Bee (Worker 2: Verifier) -->
  <g transform="translate(256, 236) rotate(120) translate(0, -116)">
    <polygon points="0,0 -42,-28 -28,8 0,4" fill="#fef3c7" fill-opacity="0.25" stroke="#f59e0b" stroke-width="1.5"/>
    <polygon points="0,0 42,-28 28,8 0,4" fill="#fef3c7" fill-opacity="0.25" stroke="#f59e0b" stroke-width="1.5"/>
    <path d="M 0,-18 C 10,-18 12,14 0,22 C -12,14 -10,-18 0,-18 Z" fill="#241a12" stroke="#fbbf24" stroke-width="2"/>
    <line x1="-7" y1="-4" x2="7" y2="-4" stroke="#f59e0b" stroke-width="2"/>
    <line x1="-8" y1="4" x2="8" y2="4" stroke="#fbbf24" stroke-width="2"/>
    <line x1="-6" y1="12" x2="6" y2="12" stroke="#f59e0b" stroke-width="2"/>
  </g>

  <!-- Bottom-Left Bee (Worker 3: Gatekeeper) -->
  <g transform="translate(256, 236) rotate(240) translate(0, -116)">
    <polygon points="0,0 -42,-28 -28,8 0,4" fill="#fef3c7" fill-opacity="0.25" stroke="#f59e0b" stroke-width="1.5"/>
    <polygon points="0,0 42,-28 28,8 0,4" fill="#fef3c7" fill-opacity="0.25" stroke="#f59e0b" stroke-width="1.5"/>
    <path d="M 0,-18 C 10,-18 12,14 0,22 C -12,14 -10,-18 0,-18 Z" fill="#241a12" stroke="#fbbf24" stroke-width="2"/>
    <line x1="-7" y1="-4" x2="7" y2="-4" stroke="#f59e0b" stroke-width="2"/>
    <line x1="-8" y1="4" x2="8" y2="4" stroke="#fbbf24" stroke-width="2"/>
    <line x1="-6" y1="12" x2="6" y2="12" stroke="#f59e0b" stroke-width="2"/>
  </g>

  <!-- Pheromone Alignment Rays -->
  <g stroke="#d97706" stroke-width="1.5" stroke-dasharray="3 4" opacity="0.6">
    <line x1="256" y1="144" x2="256" y2="180" />
    <line x1="334" y1="281" x2="304" y2="264" />
    <line x1="178" y1="281" x2="208" y2="264" />
  </g>

  <!-- Wordmark -->
  <text x="256" y="470" font-family="'Space Grotesk', 'Plus Jakarta Sans', 'Segoe UI', system-ui, sans-serif" font-size="22" fill="#fbf7ee" letter-spacing="7" text-anchor="middle" font-weight="700">STIGMERIG</text>
</svg>"""

# ---------------------------------------------------------------------------
# CANDIDATE D: "Sovereign Honeycomb Seal"
# Minimalist heraldic bee emblem framed inside an amber hexagonal cell.
# ---------------------------------------------------------------------------
SVG_OPTION_D = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" width="100%" height="100%">
  <!-- Background: Deepest Obsidian Espresso -->
  <rect width="512" height="512" rx="112" fill="#13110f" />

  <!-- Outer Hexagonal Apiary Frame -->
  <polygon points="256,36 430,136 430,336 256,436 82,336 82,136" fill="none" stroke="#2e241b" stroke-width="2" />
  <polygon points="256,52 416,144 416,328 256,420 96,328 96,144" fill="none" stroke="#6b533a" stroke-width="1.5" stroke-dasharray="6 6" opacity="0.7"/>

  <!-- Inner Amber Honey Cell Ground -->
  <polygon points="256,76 394,156 394,316 256,396 118,316 118,156" fill="#1e1812" stroke="#d97706" stroke-width="3" />

  <!-- Swept Geometric Wings -->
  <g fill="#fef3c7" fill-opacity="0.18" stroke="#fbbf24" stroke-width="2">
    <polygon points="256,216 142,142 168,226 238,236" />
    <polygon points="238,236 156,238 184,286 244,258" />
    <polygon points="256,216 370,142 344,226 274,236" />
    <polygon points="274,236 356,238 328,286 268,258" />
  </g>

  <!-- Wing Vein Linework -->
  <g stroke="#f59e0b" stroke-width="1.5" opacity="0.7">
    <line x1="256" y1="216" x2="168" y2="226" />
    <line x1="256" y1="216" x2="344" y2="226" />
  </g>

  <!-- Central Bee Monolith Body -->
  <path d="M 250,154 Q 240,126 226,124" fill="none" stroke="#fbbf24" stroke-width="2.5" stroke-linecap="round"/>
  <path d="M 262,154 Q 272,126 286,124" fill="none" stroke="#fbbf24" stroke-width="2.5" stroke-linecap="round"/>
  <circle cx="226" cy="124" r="3" fill="#fbbf24" />
  <circle cx="286" cy="124" r="3" fill="#fbbf24" />

  <polygon points="256,146 270,166 242,166" fill="#fbbf24" stroke="#13110f" stroke-width="1.5" />
  <polygon points="256,168 280,204 256,236 232,204" fill="#d97706" stroke="#fbf7ee" stroke-width="2"/>
  <circle cx="256" cy="204" r="5" fill="#fbf7ee" />

  <g stroke="#13110f" stroke-width="1.5">
    <polygon points="234,240 278,240 274,264 238,264" fill="#fbbf24" />
    <polygon points="238,266 274,266 270,288 242,288" fill="#241a12" />
    <polygon points="242,290 270,290 264,312 248,312" fill="#fbbf24" />
    <polygon points="248,314 264,314 256,342" fill="#d97706" stroke="#fbbf24" stroke-width="1" />
  </g>

  <!-- Wordmark -->
  <text x="256" y="470" font-family="'Space Grotesk', 'Plus Jakarta Sans', 'Segoe UI', system-ui, sans-serif" font-size="22" fill="#fbf7ee" letter-spacing="7" text-anchor="middle" font-weight="700">STIGMERIG</text>
</svg>"""

CANDIDATES = {
    "option_a": {
        "title": "Apiary Honeycomb Architecture",
        "description": "Surveying worker bee positioned over active comb cells with wax deposition cues.",
        "svg": SVG_OPTION_A,
    },
    "option_b": {
        "title": "Waggle Dance Vector Matrix",
        "description": "Figure-8 waggle dance navigation run with sun compass orientation and observer nodes.",
        "svg": SVG_OPTION_B,
    },
    "option_c": {
        "title": "The Comb Guild (Triad)",
        "description": "Three coordinating builder bees executing around a single central hexagonal comb cell.",
        "svg": SVG_OPTION_C,
    },
    "option_d": {
        "title": "Sovereign Honeycomb Seal",
        "description": "Geometric heraldic bee inside an amber honey cell with golden ratio wings.",
        "svg": SVG_OPTION_D,
    },
}


def sha256_file(filepath: Path) -> str:
    """Compute SHA-256 hex digest for a file on disk."""
    hasher = hashlib.sha256()
    with open(filepath, "rb") as f:
        while chunk := f.read(65536):
            hasher.update(chunk)
    return hasher.hexdigest()


def render_svg_to_png(page, svg_code: str, output_png: Path) -> None:
    """Render square 1024x1024 brand mark SVG to PNG via Chromium."""
    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  * {{ box-sizing: border-box; }}
  html, body {{
    margin: 0;
    padding: 0;
    width: 1024px;
    height: 1024px;
    background: transparent;
    overflow: hidden;
  }}
  svg {{
    width: 1024px;
    height: 1024px;
    display: block;
  }}
</style>
</head>
<body>
{svg_code}
</body>
</html>"""
    page.set_viewport_size({"width": 1024, "height": 1024})
    page.set_content(html)
    page.screenshot(path=str(output_png), omit_background=True)


def build_social_preview_html(mark_svg: str) -> str:
    """Build 1280x640 contributor-neutral hero and OpenGraph banner HTML."""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<style>
  :root {{
    --brand-hue: 65;
    --bg-canvas: oklch(0.185 0.022 var(--brand-hue));
    --bg-glow-a: oklch(0.27 0.052 var(--brand-hue) / 0.42);
    --bg-glow-b: oklch(0.24 0.040 var(--brand-hue) / 0.28);
    --bg-squircle: oklch(0.145 0.015 var(--brand-hue));
    --border-squircle: oklch(0.32 0.042 var(--brand-hue));
    --border-subtle: oklch(0.30 0.028 var(--brand-hue) / 0.85);
    --pill-bg: oklch(0.21 0.032 var(--brand-hue) / 0.72);
    --pill-border: oklch(0.62 0.145 var(--brand-hue) / 0.52);
    --chip-bg: oklch(0.162 0.018 var(--brand-hue));
    --chip-border: oklch(0.32 0.036 var(--brand-hue));
    --text-ivory: oklch(0.968 0.012 var(--brand-hue));
    --text-amber: oklch(0.775 0.162 var(--brand-hue));
    --text-amber-bright: oklch(0.835 0.155 var(--brand-hue));
    --text-body: oklch(0.835 0.016 var(--brand-hue));
    --text-muted: oklch(0.70 0.020 var(--brand-hue));
    --shadow-deep: oklch(0.08 0.012 var(--brand-hue) / 0.72);
    --font-sans: 'Space Grotesk', 'Plus Jakarta Sans', 'Segoe UI', system-ui, sans-serif;
    --font-mono: 'JetBrains Mono', 'Cascadia Mono', Consolas, monospace;
  }}

  * {{
    box-sizing: border-box;
    margin: 0;
    padding: 0;
  }}

  html, body {{
    width: 1280px;
    height: 640px;
    overflow: hidden;
    font-family: var(--font-sans);
    background-color: var(--bg-canvas);
    background-image:
      radial-gradient(ellipse 560px 420px at 22% 52%, var(--bg-glow-a), transparent 72%),
      radial-gradient(ellipse 620px 360px at 78% 18%, var(--bg-glow-b), transparent 70%);
    color: var(--text-ivory);
    -webkit-font-smoothing: antialiased;
  }}

  .canvas {{
    width: 1280px;
    height: 640px;
    padding: 64px 84px;
    display: flex;
    align-items: center;
    gap: 58px;
  }}

  .left-col {{
    flex: 0 0 336px;
    display: flex;
    align-items: center;
    justify-content: center;
  }}

  .mark-card {{
    width: 336px;
    height: 336px;
    border-radius: 74px;
    background: var(--bg-squircle);
    border: 1.5px solid var(--border-squircle);
    box-shadow: 0 26px 60px var(--shadow-deep);
    overflow: hidden;
    display: flex;
    align-items: center;
    justify-content: center;
  }}

  .mark-card svg {{
    width: 100%;
    height: 100%;
    display: block;
  }}

  .right-col {{
    flex: 1;
    height: 100%;
    display: flex;
    flex-direction: column;
    justify-content: space-between;
  }}

  .top-meta {{
    display: flex;
    align-items: center;
    justify-content: space-between;
  }}

  .overline-pill {{
    display: inline-flex;
    align-items: center;
    padding: 7px 18px;
    border-radius: 999px;
    background: var(--pill-bg);
    border: 1px solid var(--pill-border);
    color: var(--text-amber-bright);
    font-size: 12px;
    font-weight: 700;
    letter-spacing: 0.11em;
    text-transform: uppercase;
  }}

  .oss-label {{
    display: inline-flex;
    align-items: center;
    gap: 9px;
    color: var(--text-body);
    font-size: 14px;
    font-weight: 500;
    letter-spacing: 0.01em;
  }}

  .oss-label svg {{
    width: 18px;
    height: 18px;
    fill: currentColor;
    opacity: 0.9;
  }}

  .hero-block {{
    display: flex;
    flex-direction: column;
    margin: auto 0;
    padding: 8px 0;
  }}

  .hero-title {{
    font-size: 72px;
    font-weight: 800;
    line-height: 1.02;
    letter-spacing: -0.032em;
    color: var(--text-ivory);
  }}

  .hero-subtitle {{
    margin-top: 10px;
    font-size: 23.5px;
    font-weight: 700;
    line-height: 1.25;
    letter-spacing: -0.01em;
    color: var(--text-amber);
  }}

  .hero-summary {{
    margin-top: 16px;
    font-size: 16px;
    font-weight: 450;
    line-height: 1.54;
    color: var(--text-body);
    max-width: 696px;
  }}

  .bottom-block {{
    display: flex;
    flex-direction: column;
    gap: 18px;
  }}

  .divider {{
    width: 100%;
    height: 1px;
    background: var(--border-subtle);
  }}

  .bottom-row {{
    display: flex;
    align-items: flex-end;
    justify-content: space-between;
    gap: 20px;
  }}

  .cap-grid {{
    display: grid;
    grid-template-columns: repeat(3, max-content);
    gap: 8px 10px;
  }}

  .cap-chip {{
    display: inline-flex;
    align-items: center;
    justify-content: center;
    padding: 6px 12px;
    border-radius: 7px;
    background: var(--chip-bg);
    border: 1px solid var(--chip-border);
    font-family: var(--font-sans);
    font-size: 12.5px;
    font-weight: 600;
    letter-spacing: 0.01em;
    color: var(--text-ivory);
    white-space: nowrap;
  }}

  .project-meta {{
    display: flex;
    flex-direction: column;
    align-items: flex-end;
    justify-content: flex-end;
    text-align: right;
    gap: 4px;
    padding-bottom: 2px;
  }}

  .meta-primary {{
    font-family: var(--font-mono);
    font-size: 13.5px;
    font-weight: 700;
    color: var(--text-ivory);
    letter-spacing: -0.01em;
    white-space: nowrap;
  }}

  .meta-secondary {{
    font-size: 13px;
    font-weight: 500;
    color: var(--text-muted);
    white-space: nowrap;
  }}
</style>
</head>
<body>
  <div class="canvas">
    <div class="left-col">
      <div class="mark-card">
        {mark_svg}
      </div>
    </div>
    <div class="right-col">
      <div class="top-meta">
        <div class="overline-pill">AUTONOMOUS AGENT CONTROL PLANE</div>
        <div class="oss-label">
          <svg viewBox="0 0 16 16" aria-hidden="true">
            <path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38
            0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52
            -.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2
            -3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82
            .64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08
            2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01
            1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z"/>
          </svg>
          <span>Open Source Infrastructure</span>
        </div>
      </div>

      <div class="hero-block">
        <h1 class="hero-title">StigmeRig</h1>
        <div class="hero-subtitle">Multi-Agent Coordination &amp; Orchestration Engine</div>
        <p class="hero-summary">
          Local-first Python control plane enforcing atomic CAS task leases, isolated git worktree waves,
          Anti-DMV cognitive governance, and WORM SHA-256 audit chains.
        </p>
      </div>

      <div class="bottom-block">
        <div class="divider"></div>
        <div class="bottom-row">
          <div class="cap-grid">
            <span class="cap-chip">SQLite WAL Engine</span>
            <span class="cap-chip">Atomic CAS Leases</span>
            <span class="cap-chip">Worktree Waves</span>
            <span class="cap-chip">WORM Audit Trails</span>
            <span class="cap-chip">Anti-DMV Telemetry</span>
            <span class="cap-chip">Trap Deduplication</span>
          </div>
          <div class="project-meta">
            <div class="meta-primary">MIT License &bull; Python 3.10+</div>
            <div class="meta-secondary">Zero Cloud Dependencies</div>
          </div>
        </div>
      </div>
    </div>
  </div>
</body>
</html>"""


def build_architecture_html() -> str:
    """Build 1440x810 four-stage control-plane schematic HTML with >= 10% safe padding."""
    connector_svg = """
    <div class="stage-connector" aria-hidden="true">
      <svg viewBox="0 0 28 64" width="28" height="64">
        <line x1="2" y1="32" x2="21" y2="32" class="conn-line" />
        <polygon points="26,32 18,27 18,37" class="conn-arrow" />
        <circle cx="3" cy="32" r="2.5" class="conn-dot" />
      </svg>
    </div>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<style>
  :root {{
    --brand-hue: 65;
    --bg-canvas: oklch(0.175 0.020 var(--brand-hue));
    --bg-glow-top: oklch(0.255 0.048 var(--brand-hue) / 0.34);
    --bg-glow-bottom: oklch(0.225 0.036 var(--brand-hue) / 0.24);
    --bg-card: oklch(0.205 0.022 var(--brand-hue));
    --bg-card-top: oklch(0.232 0.028 var(--brand-hue));
    --bg-strip: oklch(0.155 0.018 var(--brand-hue));
    --bg-code: oklch(0.255 0.035 var(--brand-hue));
    --bg-badge: oklch(0.150 0.018 var(--brand-hue));
    --border-card: oklch(0.325 0.038 var(--brand-hue));
    --border-accent: oklch(0.58 0.140 var(--brand-hue) / 0.55);
    --border-subtle: oklch(0.285 0.026 var(--brand-hue));
    --text-ivory: oklch(0.968 0.012 var(--brand-hue));
    --text-amber: oklch(0.790 0.158 var(--brand-hue));
    --text-amber-soft: oklch(0.865 0.115 var(--brand-hue));
    --text-body: oklch(0.865 0.015 var(--brand-hue));
    --text-muted: oklch(0.695 0.022 var(--brand-hue));
    --shadow-card: oklch(0.09 0.012 var(--brand-hue) / 0.55);
    --font-sans: 'Space Grotesk', 'Plus Jakarta Sans', 'Segoe UI', system-ui, sans-serif;
    --font-mono: 'JetBrains Mono', 'Cascadia Mono', Consolas, monospace;
  }}

  * {{
    box-sizing: border-box;
    margin: 0;
    padding: 0;
  }}

  html, body {{
    width: 1440px;
    height: 810px;
    overflow: hidden;
    font-family: var(--font-sans);
    background-color: var(--bg-canvas);
    background-image:
      radial-gradient(ellipse 760px 340px at 50% 8%, var(--bg-glow-top), transparent 72%),
      radial-gradient(ellipse 680px 300px at 50% 94%, var(--bg-glow-bottom), transparent 72%);
    color: var(--text-ivory);
    -webkit-font-smoothing: antialiased;
  }}

  /* 10% safe edge padding: 82px vertical (10.1%), 144px horizontal (10.0%) */
  .frame {{
    width: 1440px;
    height: 810px;
    padding: 82px 144px;
    display: flex;
    flex-direction: column;
    justify-content: space-between;
  }}

  .header {{
    display: flex;
    align-items: flex-end;
    justify-content: space-between;
    gap: 24px;
    padding-bottom: 18px;
    border-bottom: 1px solid var(--border-subtle);
  }}

  .header-left {{
    display: flex;
    flex-direction: column;
    gap: 7px;
  }}

  .overline {{
    display: inline-flex;
    align-items: center;
    gap: 8px;
    font-size: 12px;
    font-weight: 700;
    letter-spacing: 0.13em;
    text-transform: uppercase;
    color: var(--text-amber);
  }}

  .overline-hex {{
    width: 12px;
    height: 12px;
    stroke: var(--text-amber);
    fill: none;
    stroke-width: 2;
  }}

  .title {{
    font-size: 30px;
    font-weight: 800;
    line-height: 1.12;
    letter-spacing: -0.024em;
    color: var(--text-ivory);
  }}

  .header-badge {{
    padding: 8px 15px;
    border-radius: 999px;
    background: var(--bg-badge);
    border: 1px solid var(--border-accent);
    font-family: var(--font-mono);
    font-size: 12px;
    font-weight: 600;
    color: var(--text-amber-soft);
    white-space: nowrap;
  }}

  .pipeline {{
    display: flex;
    align-items: stretch;
    justify-content: space-between;
    height: 436px;
  }}

  .stage-card {{
    flex: 1;
    min-width: 0;
    background: linear-gradient(180deg, var(--bg-card-top) 0%, var(--bg-card) 38%);
    border: 1px solid var(--border-card);
    border-top: 2.5px solid var(--text-amber);
    border-radius: 14px;
    padding: 20px 18px 18px 18px;
    box-shadow: 0 18px 38px var(--shadow-card);
    display: flex;
    flex-direction: column;
  }}

  .stage-top {{
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
  }}

  .stage-index {{
    font-family: var(--font-mono);
    font-size: 11.5px;
    font-weight: 700;
    letter-spacing: 0.08em;
    color: var(--text-amber);
  }}

  .stage-glyph {{
    width: 22px;
    height: 22px;
    stroke: var(--text-amber);
    fill: none;
    stroke-width: 1.75;
    opacity: 0.92;
  }}

  .stage-name {{
    margin-top: 10px;
    font-size: 12.6px;
    font-weight: 800;
    line-height: 1.25;
    letter-spacing: 0.015em;
    text-transform: uppercase;
    color: var(--text-ivory);
    white-space: nowrap;
  }}

  .artifact-pill {{
    margin-top: 11px;
    align-self: flex-start;
    padding: 5px 10px;
    border-radius: 6px;
    background: var(--bg-badge);
    border: 1px solid var(--border-card);
    font-family: var(--font-mono);
    font-size: 11.5px;
    font-weight: 600;
    color: var(--text-amber-soft);
    white-space: nowrap;
  }}

  .card-rule {{
    width: 100%;
    height: 1px;
    background: var(--border-subtle);
    margin: 16px 0;
  }}

  .bullet-list {{
    list-style: none;
    display: flex;
    flex-direction: column;
    gap: 14px;
    flex: 1;
  }}

  .bullet-item {{
    display: flex;
    align-items: flex-start;
    gap: 9px;
    font-size: 12.6px;
    font-weight: 480;
    line-height: 1.48;
    color: var(--text-body);
  }}

  .bullet-dot {{
    width: 5px;
    height: 5px;
    border-radius: 50%;
    background: var(--text-amber);
    margin-top: 6.5px;
    flex-shrink: 0;
  }}

  code {{
    font-family: var(--font-mono);
    font-size: 10.8px;
    font-weight: 600;
    padding: 1px 4.5px;
    border-radius: 4px;
    background: var(--bg-code);
    color: var(--text-ivory);
    white-space: nowrap;
  }}

  .card-footer {{
    margin-top: 14px;
    padding-top: 11px;
    border-top: 1px solid var(--border-subtle);
    display: flex;
    align-items: center;
    justify-content: space-between;
    font-family: var(--font-mono);
    font-size: 10.8px;
    font-weight: 600;
    letter-spacing: 0.03em;
    color: var(--text-muted);
  }}

  .card-footer strong {{
    color: var(--text-amber-soft);
    font-weight: 700;
  }}

  .stage-connector {{
    flex: 0 0 28px;
    display: flex;
    align-items: center;
    justify-content: center;
  }}

  .conn-line {{
    stroke: var(--text-amber);
    stroke-width: 1.8;
    stroke-dasharray: 3 3;
  }}

  .conn-arrow {{
    fill: var(--text-amber);
  }}

  .conn-dot {{
    fill: var(--text-amber-soft);
  }}

  .defense-strip {{
    background: var(--bg-strip);
    border: 1px solid var(--border-card);
    border-radius: 11px;
    padding: 13px 22px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 14px;
  }}

  .strip-tag {{
    font-family: var(--font-mono);
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.10em;
    text-transform: uppercase;
    color: var(--text-amber);
    padding: 4px 9px;
    border-radius: 5px;
    background: var(--bg-card);
    border: 1px solid var(--border-accent);
    white-space: nowrap;
  }}

  .strip-items {{
    flex: 1;
    display: flex;
    align-items: center;
    justify-content: space-around;
    font-size: 13px;
    font-weight: 600;
    color: var(--text-ivory);
  }}

  .strip-sep {{
    color: var(--text-amber);
    font-size: 12px;
  }}
</style>
</head>
<body>
  <div class="frame">
    <header class="header">
      <div class="header-left">
        <div class="overline">
          <svg class="overline-hex" viewBox="0 0 16 16">
            <polygon points="8,1 14.5,4.75 14.5,11.25 8,15 1.5,11.25 1.5,4.75" />
          </svg>
          <span>DETERMINISTIC STIGMERGIC ARCHITECTURE</span>
        </div>
        <h1 class="title">Four-Stage Coordination &amp; Governance Pipeline</h1>
      </div>
      <div class="header-badge">SQLite WAL &bull; Git Worktrees &bull; SHA-256 WORM</div>
    </header>

    <section class="pipeline">
      <!-- Stage 01 -->
      <article class="stage-card">
        <div class="stage-top">
          <span class="stage-index">STAGE 01</span>
          <svg class="stage-glyph" viewBox="0 0 24 24">
            <polygon points="12,2.5 20,7 20,17 12,21.5 4,17 4,7" />
            <circle cx="12" cy="12" r="2.8" />
          </svg>
        </div>
        <h2 class="stage-name">01 / ICM &amp; Atomic Queue</h2>
        <div class="artifact-pill">.stigmerig/queue.db</div>
        <div class="card-rule"></div>
        <ul class="bullet-list">
          <li class="bullet-item">
            <span class="bullet-dot"></span>
            <span>Scaffolds <code>AGENTS.md</code>, <code>CONTEXT.md</code>, and <code>rig.json</code></span>
          </li>
          <li class="bullet-item">
            <span class="bullet-dot"></span>
            <span>Priority queue with atomic CAS <code>version</code> check</span>
          </li>
          <li class="bullet-item">
            <span class="bullet-dot"></span>
            <span>Expiring <code>--ttl</code> leases prevent split-brain claims</span>
          </li>
        </ul>
        <div class="card-footer">
          <span>MITIGATES</span>
          <strong>Split-Brain Contention</strong>
        </div>
      </article>

      {connector_svg}

      <!-- Stage 02 -->
      <article class="stage-card">
        <div class="stage-top">
          <span class="stage-index">STAGE 02</span>
          <svg class="stage-glyph" viewBox="0 0 24 24">
            <circle cx="6" cy="6" r="2.5" />
            <circle cx="6" cy="18" r="2.5" />
            <circle cx="18" cy="12" r="2.5" />
            <path d="M8.5,6 H13 C15.5,6 16,8.5 18,9.5 M8.5,18 H13 C15.5,18 16,15.5 18,14.5" />
          </svg>
        </div>
        <h2 class="stage-name">02 / Worktree Waves</h2>
        <div class="artifact-pill">stigmerig.wave</div>
        <div class="card-rule"></div>
        <ul class="bullet-list">
          <li class="bullet-item">
            <span class="bullet-dot"></span>
            <span><code>3&ndash;5</code> concurrent task cap via <code>WaveRunner</code></span>
          </li>
          <li class="bullet-item">
            <span class="bullet-dot"></span>
            <span>Isolated <code>.worktrees/&lt;ID&gt;</code> git branches</span>
          </li>
          <li class="bullet-item">
            <span class="bullet-dot"></span>
            <span>Clean-tree pre-merge check &amp; conflict abort</span>
          </li>
        </ul>
        <div class="card-footer">
          <span>MITIGATES</span>
          <strong>Branch Collisions</strong>
        </div>
      </article>

      {connector_svg}

      <!-- Stage 03 -->
      <article class="stage-card">
        <div class="stage-top">
          <span class="stage-index">STAGE 03</span>
          <svg class="stage-glyph" viewBox="0 0 24 24">
            <circle cx="8" cy="12" r="4.5" />
            <circle cx="16" cy="12" r="4.5" />
            <line x1="12" y1="4" x2="12" y2="20" />
          </svg>
        </div>
        <h2 class="stage-name">03 / Separation of Duties</h2>
        <div class="artifact-pill">Verifier Gate</div>
        <div class="card-rule"></div>
        <ul class="bullet-list">
          <li class="bullet-item">
            <span class="bullet-dot"></span>
            <span>Rejects missing or aliased self-approvals</span>
          </li>
          <li class="bullet-item">
            <span class="bullet-dot"></span>
            <span>Seat-component parsing across <code>/</code>, <code>@</code>, <code>:</code></span>
          </li>
          <li class="bullet-item">
            <span class="bullet-dot"></span>
            <span>Workflow rails: <code>diff -&gt; test -&gt; policy</code></span>
          </li>
        </ul>
        <div class="card-footer">
          <span>MITIGATES</span>
          <strong>Self-Approval Collusion</strong>
        </div>
      </article>

      {connector_svg}

      <!-- Stage 04 -->
      <article class="stage-card">
        <div class="stage-top">
          <span class="stage-index">STAGE 04</span>
          <svg class="stage-glyph" viewBox="0 0 24 24">
            <rect x="4" y="5" width="16" height="5" rx="1.5" />
            <rect x="4" y="14" width="16" height="5" rx="1.5" />
            <line x1="12" y1="10" x2="12" y2="14" />
          </svg>
        </div>
        <h2 class="stage-name">04 / WORM Audit &amp; Telemetry</h2>
        <div class="artifact-pill">audit.db + traps.json</div>
        <div class="card-rule"></div>
        <ul class="bullet-list">
          <li class="bullet-item">
            <span class="bullet-dot"></span>
            <span>SQLite triggers block <code>UPDATE</code> &amp; <code>DELETE</code></span>
          </li>
          <li class="bullet-item">
            <span class="bullet-dot"></span>
            <span>Anti-DMV gate (<code>verify_only_ratio &lt;= 0.30</code>)</span>
          </li>
          <li class="bullet-item">
            <span class="bullet-dot"></span>
            <span>Deduplicated blocker traps (<code>occurrences &gt;= 3</code>)</span>
          </li>
        </ul>
        <div class="card-footer">
          <span>MITIGATES</span>
          <strong>Tampering &amp; DMV Stall</strong>
        </div>
      </article>
    </section>

    <footer class="defense-strip">
      <span class="strip-tag">Guarantees</span>
      <div class="strip-items">
        <span>Zero Cloud Brokers</span>
        <span class="strip-sep">&bull;</span>
        <span>Tier A / Tier B Schema Interop</span>
        <span class="strip-sep">&bull;</span>
        <span><code>--rig-dir</code> Subproject Isolation</span>
        <span class="strip-sep">&bull;</span>
        <span>NIST AI RMF &amp; SP 800-218 Aligned</span>
      </div>
    </footer>
  </div>
</body>
</html>"""


def render_html_card(page, html: str, width: int, height: int, output_png: Path) -> None:
    """Render parameterized HTML card to PNG at exact viewport dimensions."""
    page.set_viewport_size({"width": width, "height": height})
    page.set_content(html)
    page.screenshot(path=str(output_png), omit_background=False)


def main() -> None:
    """Compile candidate marks, official avatar, social preview, and architecture diagram."""
    parser = argparse.ArgumentParser(description="Compile StigmeRig brand and architecture assets.")
    parser.add_argument(
        "--select",
        choices=["option_a", "option_b", "option_c", "option_d"],
        default="option_c",
        help="Candidate mark to promote into assets/ (default: option_c)",
    )
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent.parent
    scratch_dir = Path(
        os.environ.get(
            "STIGMERIG_BRAND_SCRATCH",
            str(Path(tempfile.gettempdir()) / "stigmerig-brand-candidates"),
        )
    )
    scratch_dir.mkdir(parents=True, exist_ok=True)

    assets_dir = repo_root / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)

    chosen = CANDIDATES[args.select]
    social_png = assets_dir / "stigmerig-social-preview.png"
    arch_png = assets_dir / "stigmerig-architecture.png"

    print(f"Rendering 4 brand mark candidates into {scratch_dir}...")
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1024, "height": 1024})

        for opt_key, opt_data in CANDIDATES.items():
            svg_path = scratch_dir / f"{opt_key}.svg"
            png_path = scratch_dir / f"{opt_key}.png"

            svg_path.write_text(opt_data["svg"], encoding="utf-8", newline="\n")
            render_svg_to_png(page, opt_data["svg"], png_path)
            print(f"  [candidate] {opt_key}: {opt_data['title']} -> {png_path.name}")

        print("Rendering contributor-neutral social preview (1280x640)...")
        render_html_card(page, build_social_preview_html(chosen["svg"]), 1280, 640, social_png)

        print("Rendering four-stage architecture schematic (1440x810)...")
        render_html_card(page, build_architecture_html(), 1440, 810, arch_png)

        browser.close()

    print(f"\nPromoting chosen mark '{args.select}' ({chosen['title']}) to {assets_dir}...")

    logo_svg = assets_dir / "stigmerig-logo.svg"
    logo_svg.write_text(chosen["svg"], encoding="utf-8", newline="\n")

    avatar_png = assets_dir / "stigmerig-avatar.png"
    src_png = scratch_dir / f"{args.select}.png"
    avatar_png.write_bytes(src_png.read_bytes())

    manifest = {
        "slug": "stigmerig-brand-avatar",
        "concept": f"StigmeRig official brand mark, social preview, and architecture schematic - {chosen['title']}",
        "archetype": "instrumentation",
        "engine": "html",
        "provenance": {
            "generator": "html-playwright",
            "source_script": "scripts/build_branding.py",
            "viewport": [1024, 1024],
            "viewports": {
                "stigmerig-avatar.png": [1024, 1024],
                "stigmerig-social-preview.png": [1280, 640],
                "stigmerig-architecture.png": [1440, 810],
            },
            "renderer": "chromium-playwright",
        },
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "files": {
            "stigmerig-logo.svg": sha256_file(logo_svg),
            "stigmerig-avatar.png": sha256_file(avatar_png),
            "stigmerig-social-preview.png": sha256_file(social_png),
            "stigmerig-architecture.png": sha256_file(arch_png),
        },
    }

    manifest_file = assets_dir / "manifest.json"
    manifest_file.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n")

    print("Brand compilation complete:")
    print(f"  - Logo SVG: {logo_svg}")
    print(f"  - Avatar PNG: {avatar_png}")
    print(f"  - Social Preview PNG: {social_png}")
    print(f"  - Architecture PNG: {arch_png}")
    print(f"  - Manifest: {manifest_file}")


if __name__ == "__main__":
    main()
