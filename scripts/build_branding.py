#!/usr/bin/env python3
"""
scripts/build_branding.py

Deterministic brand asset compiler for StigmeRig.
Renders 4 bee & beehive stigmergic candidate marks into
scratch/stigmerig-brand-candidates/ and promotes the selected mark (default: option_a)
to assets/ with manifest.json.
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
# Geometric worker bee surveying and building an interlocking honeycomb comb.
# Palette: Propolis charcoal (#161412), raw honey gold (#d97706),
# beeswax amber (#f59e0b), pollen yellow (#fbbf24), cream ivory (#fbf7ee).
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
    <!-- Wings: Translucent Hexagonal Facets (Beeswax Parchment) -->
    <!-- Left Forewing -->
    <polygon points="0,-16 -78,-72 -112,-48 -32,2" fill="#fef3c7" fill-opacity="0.22" stroke="#fbbf24" stroke-width="2" />
    <line x1="-32" y1="-30" x2="-86" y2="-56" stroke="#f59e0b" stroke-width="1.5" opacity="0.6"/>
    <!-- Left Hindwing -->
    <polygon points="-8,-4 -84,-12 -68,22 -16,8" fill="#fef3c7" fill-opacity="0.16" stroke="#f59e0b" stroke-width="1.5" />

    <!-- Right Forewing -->
    <polygon points="0,-16 78,-72 112,-48 32,2" fill="#fef3c7" fill-opacity="0.22" stroke="#fbbf24" stroke-width="2" />
    <line x1="32" y1="-30" x2="86" y2="-56" stroke="#f59e0b" stroke-width="1.5" opacity="0.6"/>
    <!-- Right Hindwing -->
    <polygon points="8,-4 84,-12 68,22 16,8" fill="#fef3c7" fill-opacity="0.16" stroke="#f59e0b" stroke-width="1.5" />

    <!-- Head & Antennae -->
    <circle cx="0" cy="-44" r="11" fill="#292017" stroke="#f59e0b" stroke-width="2" />
    <circle cx="-5" cy="-46" r="3" fill="#fbf7ee" />
    <circle cx="5" cy="-46" r="3" fill="#fbf7ee" />
    <path d="M -4,-54 Q -12,-72 -22,-74" fill="none" stroke="#f59e0b" stroke-width="2" stroke-linecap="round"/>
    <path d="M 4,-54 Q 12,-72 22,-74" fill="none" stroke="#f59e0b" stroke-width="2" stroke-linecap="round"/>

    <!-- Thorax: Propolis Bronze with Core Amber Jewel -->
    <path d="M 0,-34 C 18,-34 22,-8 0,-4 C -22,-8 -18,-34 0,-34 Z" fill="#382a1d" stroke="#f59e0b" stroke-width="2.5" />
    <circle cx="0" cy="-19" r="6" fill="#f59e0b" />

    <!-- Abdomen: Striped Black & Pollen Amber Segments -->
    <!-- Segment 1 -->
    <path d="M -16,-2 C -18,12 18,12 16,-2 Z" fill="#f59e0b" stroke="#161412" stroke-width="1.5" />
    <!-- Segment 2 -->
    <path d="M -15,10 C -16,22 16,22 15,10 Z" fill="#241a12" stroke="#f59e0b" stroke-width="1" />
    <!-- Segment 3 -->
    <path d="M -13,20 C -14,32 14,32 13,20 Z" fill="#fbbf24" stroke="#161412" stroke-width="1.5" />
    <!-- Segment 4 -->
    <path d="M -10,30 C -10,40 10,40 10,30 Z" fill="#241a12" stroke="#f59e0b" stroke-width="1" />
    <!-- Stinger Apex -->
    <polygon points="-7,38 7,38 0,56" fill="#f59e0b" stroke="#241a12" stroke-width="1" />
  </g>

  <!-- StigmeRig Wordmark -->
  <text x="256" y="470" font-family="'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif" font-size="22" fill="#fbf7ee" letter-spacing="7" text-anchor="middle" font-weight="700">STIGMERIG</text>
</svg>"""

# ---------------------------------------------------------------------------
# CANDIDATE B: "Waggle Dance Vector Matrix"
# The biological archetype of decentralized coordination: figure-8 waggle dance
# trajectories encoding angle and distance, surrounded by honeycomb cells.
# ---------------------------------------------------------------------------
SVG_OPTION_B = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" width="100%" height="100%">
  <!-- Background: Deep Warm Espresso Slate -->
  <rect width="512" height="512" rx="112" fill="#141210" />

  <!-- Outer Dial: Sun Compass Reticle (Used in Bee Navigation) -->
  <circle cx="256" cy="236" r="192" fill="none" stroke="#2e261f" stroke-width="2" />
  <circle cx="256" cy="236" r="176" fill="none" stroke="#4a3b2c" stroke-width="1" stroke-dasharray="4 8" opacity="0.6"/>

  <!-- Sun Reference Line (Azimuth Orientation) -->
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

  <!-- Figure-8 Waggle Dance Path (Bilateral Loops) -->
  <!-- Left Dance Loop -->
  <path d="M 256,160 C 140,160 140,312 256,312" fill="none" stroke="#d97706" stroke-width="2.5" stroke-linecap="round" stroke-dasharray="5 5" opacity="0.75"/>
  <!-- Right Dance Loop -->
  <path d="M 256,312 C 372,312 372,160 256,160" fill="none" stroke="#d97706" stroke-width="2.5" stroke-linecap="round" stroke-dasharray="5 5" opacity="0.75"/>

  <!-- Waggle Run Center Corridor (Vibrating Vector Spine) -->
  <path d="M 256,312 L 256,160" stroke="#f59e0b" stroke-width="4" stroke-linecap="round"/>
  <!-- Waggle Modulation Ticks -->
  <g stroke="#fbbf24" stroke-width="2" stroke-linecap="round">
    <line x1="244" y1="290" x2="268" y2="290" />
    <line x1="240" y1="270" x2="272" y2="270" />
    <line x1="236" y1="250" x2="276" y2="250" />
    <line x1="240" y1="230" x2="272" y2="230" />
    <line x1="244" y1="210" x2="268" y2="210" />
    <line x1="248" y1="190" x2="264" y2="190" />
  </g>

  <!-- Dancing Bee Silhouette Ascending the Waggle Run -->
  <g transform="translate(256, 236)">
    <!-- Angular Wings -->
    <polygon points="0,-8 -54,-40 -40,4 0,0" fill="#fef3c7" fill-opacity="0.3" stroke="#fbbf24" stroke-width="1.5" />
    <polygon points="0,-8 54,-40 40,4 0,0" fill="#fef3c7" fill-opacity="0.3" stroke="#fbbf24" stroke-width="1.5" />
    <!-- Bee Body -->
    <ellipse cx="0" cy="0" rx="9" ry="18" fill="#141210" stroke="#f59e0b" stroke-width="2.5" />
    <line x1="-7" y1="-4" x2="7" y2="-4" stroke="#fbbf24" stroke-width="2" />
    <line x1="-8" y1="4" x2="8" y2="4" stroke="#fbbf24" stroke-width="2" />
  </g>

  <!-- Observer Bees / Agent Followers Reading the Signal -->
  <g fill="#f59e0b" stroke="#141210" stroke-width="2">
    <circle cx="196" cy="200" r="7" />
    <circle cx="196" cy="272" r="7" />
    <circle cx="316" cy="200" r="7" />
    <circle cx="316" cy="272" r="7" />
  </g>

  <!-- Wordmark -->
  <text x="256" y="470" font-family="'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif" font-size="22" fill="#fbf7ee" letter-spacing="7" text-anchor="middle" font-weight="700">STIGMERIG</text>
</svg>"""

# ---------------------------------------------------------------------------
# CANDIDATE C: "The Comb Guild"
# Triad of three minimalist geometric bees collaborating around a single
# central hexagonal cell: the pure embodiment of multi-agent stigmergy.
# ---------------------------------------------------------------------------
SVG_OPTION_C = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" width="100%" height="100%">
  <!-- Background: Deep Umber Charcoal -->
  <rect width="512" height="512" rx="112" fill="#151311" />

  <!-- Outer Guild Seal -->
  <circle cx="256" cy="236" r="194" fill="none" stroke="#2c2219" stroke-width="2"/>
  <circle cx="256" cy="236" r="182" fill="none" stroke="#544130" stroke-width="1.5" stroke-dasharray="3 6"/>

  <!-- Triad Geometric Symmetry Guild (Three Bees around a Central Hexagon) -->
  <!-- Central Shared Comb Cell Being Built -->
  <polygon points="256,180 304,208 304,264 256,292 208,264 208,208" fill="#241a12" stroke="#d97706" stroke-width="3"/>
  <circle cx="256" cy="236" r="8" fill="#fbbf24" stroke="#151311" stroke-width="2"/>

  <!-- Surrounding Honeycomb Facets -->
  <g fill="none" stroke="#3d2f22" stroke-width="1.5" opacity="0.7">
    <polygon points="256,68 304,96 304,152 256,180 208,152 208,96" />
    <polygon points="352,236 400,264 400,320 352,348 304,320 304,264" />
    <polygon points="160,236 208,264 208,320 160,348 112,320 112,264" />
  </g>

  <!-- Three Coordinating Bees: Rotated at 0°, 120°, 240° around center (256, 236) -->
  <!-- Helper macro: Bee at 0° (top), distance 116 from center -->
  <!-- Top Bee (Worker 1: Builder) -->
  <g transform="translate(256, 120)">
    <!-- Wings -->
    <polygon points="0,0 -42,-28 -28,8 0,4" fill="#fef3c7" fill-opacity="0.25" stroke="#f59e0b" stroke-width="1.5"/>
    <polygon points="0,0 42,-28 28,8 0,4" fill="#fef3c7" fill-opacity="0.25" stroke="#f59e0b" stroke-width="1.5"/>
    <!-- Body -->
    <path d="M 0,-18 C 10,-18 12,14 0,22 C -12,14 -10,-18 0,-18 Z" fill="#241a12" stroke="#fbbf24" stroke-width="2"/>
    <line x1="-7" y1="-4" x2="7" y2="-4" stroke="#f59e0b" stroke-width="2"/>
    <line x1="-8" y1="4" x2="8" y2="4" stroke="#fbbf24" stroke-width="2"/>
    <line x1="-6" y1="12" x2="6" y2="12" stroke="#f59e0b" stroke-width="2"/>
  </g>

  <!-- Bottom-Right Bee (Worker 2: Verifier) rotated 120° -->
  <g transform="translate(256, 236) rotate(120) translate(0, -116)">
    <!-- Wings -->
    <polygon points="0,0 -42,-28 -28,8 0,4" fill="#fef3c7" fill-opacity="0.25" stroke="#f59e0b" stroke-width="1.5"/>
    <polygon points="0,0 42,-28 28,8 0,4" fill="#fef3c7" fill-opacity="0.25" stroke="#f59e0b" stroke-width="1.5"/>
    <!-- Body -->
    <path d="M 0,-18 C 10,-18 12,14 0,22 C -12,14 -10,-18 0,-18 Z" fill="#241a12" stroke="#fbbf24" stroke-width="2"/>
    <line x1="-7" y1="-4" x2="7" y2="-4" stroke="#f59e0b" stroke-width="2"/>
    <line x1="-8" y1="4" x2="8" y2="4" stroke="#fbbf24" stroke-width="2"/>
    <line x1="-6" y1="12" x2="6" y2="12" stroke="#f59e0b" stroke-width="2"/>
  </g>

  <!-- Bottom-Left Bee (Worker 3: Gatekeeper) rotated 240° -->
  <g transform="translate(256, 236) rotate(240) translate(0, -116)">
    <!-- Wings -->
    <polygon points="0,0 -42,-28 -28,8 0,4" fill="#fef3c7" fill-opacity="0.25" stroke="#f59e0b" stroke-width="1.5"/>
    <polygon points="0,0 42,-28 28,8 0,4" fill="#fef3c7" fill-opacity="0.25" stroke="#f59e0b" stroke-width="1.5"/>
    <!-- Body -->
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
  <text x="256" y="470" font-family="'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif" font-size="22" fill="#fbf7ee" letter-spacing="7" text-anchor="middle" font-weight="700">STIGMERIG</text>
</svg>"""

# ---------------------------------------------------------------------------
# CANDIDATE D: "Sovereign Honeycomb Seal"
# A high-craft heraldic / modern minimalist bee emblem inside a bold hexagonal
# comb cell, with golden-ratio wings and precision comb teeth.
# ---------------------------------------------------------------------------
SVG_OPTION_D = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" width="100%" height="100%">
  <!-- Background: Deepest Obsidian Espresso -->
  <rect width="512" height="512" rx="112" fill="#13110f" />

  <!-- Outer Hexagonal Apiary Frame -->
  <polygon points="256,36 430,136 430,336 256,436 82,336 82,136" fill="none" stroke="#2e241b" stroke-width="2" />
  <polygon points="256,52 416,144 416,328 256,420 96,328 96,144" fill="none" stroke="#6b533a" stroke-width="1.5" stroke-dasharray="6 6" opacity="0.7"/>

  <!-- Inner Amber Honey Cell Ground -->
  <polygon points="256,76 394,156 394,316 256,396 118,316 118,156" fill="#1e1812" stroke="#d97706" stroke-width="3" />

  <!-- Geometric Emblem: Sovereign Queen/Worker Bee Glyph -->
  <!-- Swept Geometric Wings (Golden Ratio Angles) -->
  <g fill="#fef3c7" fill-opacity="0.18" stroke="#fbbf24" stroke-width="2">
    <!-- Left Major Wing -->
    <polygon points="256,216 142,142 168,226 238,236" />
    <!-- Left Minor Wing -->
    <polygon points="238,236 156,238 184,286 244,258" />
    <!-- Right Major Wing -->
    <polygon points="256,216 370,142 344,226 274,236" />
    <!-- Right Minor Wing -->
    <polygon points="274,236 356,238 328,286 268,258" />
  </g>

  <!-- Wing Vein Linework -->
  <g stroke="#f59e0b" stroke-width="1.5" opacity="0.7">
    <line x1="256" y1="216" x2="168" y2="226" />
    <line x1="256" y1="216" x2="344" y2="226" />
  </g>

  <!-- Central Bee Monolith Body -->
  <!-- Antennae -->
  <path d="M 250,154 Q 240,126 226,124" fill="none" stroke="#fbbf24" stroke-width="2.5" stroke-linecap="round"/>
  <path d="M 262,154 Q 272,126 286,124" fill="none" stroke="#fbbf24" stroke-width="2.5" stroke-linecap="round"/>
  <circle cx="226" cy="124" r="3" fill="#fbbf24" />
  <circle cx="286" cy="124" r="3" fill="#fbbf24" />

  <!-- Head -->
  <polygon points="256,146 270,166 242,166" fill="#fbbf24" stroke="#13110f" stroke-width="1.5" />

  <!-- Thorax Core (Faceted Amber Diamond) -->
  <polygon points="256,168 280,204 256,236 232,204" fill="#d97706" stroke="#fbf7ee" stroke-width="2"/>
  <circle cx="256" cy="204" r="5" fill="#fbf7ee" />

  <!-- Abdomen: Segmented Precision Shield -->
  <g stroke="#13110f" stroke-width="1.5">
    <polygon points="234,240 278,240 274,264 238,264" fill="#fbbf24" />
    <polygon points="238,266 274,266 270,288 242,288" fill="#241a12" />
    <polygon points="242,290 270,290 264,312 248,312" fill="#fbbf24" />
    <polygon points="248,314 264,314 256,342" fill="#d97706" stroke="#fbbf24" stroke-width="1" />
  </g>

  <!-- Wordmark -->
  <text x="256" y="470" font-family="'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif" font-size="22" fill="#fbf7ee" letter-spacing="7" text-anchor="middle" font-weight="700">STIGMERIG</text>
</svg>"""

CANDIDATES = {
    "option_a": {
        "title": "Apiary Honeycomb Architecture",
        "description": "Surveying worker bee positioned over active honey comb cells with wax deposition cues.",
        "svg": SVG_OPTION_A,
    },
    "option_b": {
        "title": "Waggle Dance Vector Matrix",
        "description": "Figure-8 waggle dance navigation run with sun compass orientation and observer agent nodes.",
        "svg": SVG_OPTION_B,
    },
    "option_c": {
        "title": "The Comb Guild (Triad)",
        "description": "Three coordinating builder bees executing around a single central hexagonal comb cell.",
        "svg": SVG_OPTION_C,
    },
    "option_d": {
        "title": "Sovereign Honeycomb Seal",
        "description": "Geometric heraldic worker/queen bee inside a bold amber honey cell with golden ratio wings.",
        "svg": SVG_OPTION_D,
    },
}


def sha256_file(filepath: Path) -> str:
    hasher = hashlib.sha256()
    with open(filepath, "rb") as f:
        while chunk := f.read(65536):
            hasher.update(chunk)
    return hasher.hexdigest()


def render_svg_to_png(page, svg_code: str, output_png: Path) -> None:
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
    page.set_content(html)
    page.screenshot(path=str(output_png), omit_background=True)


def main():
    parser = argparse.ArgumentParser(description="Compile StigmeRig bee & beehive brand assets.")
    parser.add_argument(
        "--select",
        choices=["option_a", "option_b", "option_c", "option_d"],
        default="option_a",
        help="Candidate mark to promote into assets/ (default: option_a)",
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

    print(f"Rendering all 4 bee & beehive brand candidates into {scratch_dir}...")
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1024, "height": 1024})

        for opt_key, opt_data in CANDIDATES.items():
            svg_path = scratch_dir / f"{opt_key}.svg"
            png_path = scratch_dir / f"{opt_key}.png"

            svg_path.write_text(opt_data["svg"], encoding="utf-8", newline="\n")
            render_svg_to_png(page, opt_data["svg"], png_path)
            print(f"  [candidate] {opt_key}: {opt_data['title']} -> {png_path.name}")

        browser.close()

    # Promote chosen candidate
    chosen = CANDIDATES[args.select]
    print(f"\nPromoting chosen mark '{args.select}' ({chosen['title']}) to {assets_dir}...")

    logo_svg = assets_dir / "stigmerig-logo.svg"
    logo_svg.write_text(chosen["svg"], encoding="utf-8", newline="\n")

    avatar_png = assets_dir / "stigmerig-avatar.png"
    src_png = scratch_dir / f"{args.select}.png"
    avatar_png.write_bytes(src_png.read_bytes())

    # Build manifest compliant with Test_AssetProvenance.py
    manifest = {
        "slug": "stigmerig-brand-avatar",
        "concept": f"StigmeRig official organization avatar - {chosen['title']}",
        "archetype": "instrumentation",
        "engine": "html",
        "provenance": {
            "generator": "html-playwright",
            "source_script": "scripts/build_branding.py",
            "viewport": [1024, 1024],
            "renderer": "chromium-playwright"
        },
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "files": {
            "stigmerig-logo.svg": sha256_file(logo_svg),
            "stigmerig-avatar.png": sha256_file(avatar_png),
        }
    }

    manifest_file = assets_dir / "manifest.json"
    manifest_file.write_text(json.dumps(manifest, indent=2), encoding="utf-8", newline="\n")

    print(f"Brand compilation complete:")
    print(f"  - Logo SVG: {logo_svg}")
    print(f"  - Avatar PNG: {avatar_png}")
    print(f"  - Manifest: {manifest_file}")


if __name__ == "__main__":
    main()
