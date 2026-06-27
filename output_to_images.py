#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Output → WeChat Images
======================

Reads articles_output.json and generates WeChat-style images using
the NEXUS image rendering engine.

Usage:
    python output_to_images.py
    python output_to_images.py --input articles_output.json --school NYU
    python output_to_images.py --out my_images --school USC
"""

import sys
import os
import json
import argparse
from pathlib import Path
from datetime import datetime

_THIS_DIR = Path(__file__).parent.resolve()

# Keep the NEXUS font loading strategy: Chromium reads SourceHanSerifSC-VF.otf
# through a file:// URI, while start.sh also registers it with fontconfig as a
# runtime fallback on Railway.
import shutil as _shutil
import news_bot.processing.image_generator as _ig

# Prefer the top-level project font; fall back to the bundled image module copy.
_FONT_SRC = _THIS_DIR / "assets" / "fonts" / "SourceHanSerifSC-VF.otf"
if not _FONT_SRC.exists():
    _FONT_SRC = _ig.FONTS_DIR / "SourceHanSerifSC-VF.otf"

_FONT_CACHE = Path.home() / ".nexus_fonts" / "SourceHanSerifSC-VF.otf"

if _FONT_SRC.exists() and not _FONT_CACHE.exists():
    _FONT_CACHE.parent.mkdir(parents=True, exist_ok=True)
    print("[i] Caching font to ~/.nexus_fonts/ (~53MB, one-time only)...")
    _shutil.copy2(str(_FONT_SRC.resolve()), str(_FONT_CACHE))
    _ig._font_file_uri = lambda: _FONT_CACHE.as_uri()
    _font_status = f"✓ {_FONT_CACHE}"
elif _FONT_CACHE.exists():
    _ig._font_file_uri = lambda: _FONT_CACHE.as_uri()
    _font_status = f"✓ {_FONT_CACHE}"
elif _FONT_SRC.exists():
    _ig._font_file_uri = lambda: _FONT_SRC.resolve().as_uri()
    _font_status = f"✓ {_FONT_SRC}"
else:
    _font_status = "✗ 未找到（将使用系统宋体回退）"

print(f"[字体] 中文: SourceHanSerifSC (思源宋体可变字体)  →  {_font_status}")
print(f"[字体] 英文: SourceHanSerifSC 内置拉丁字形 (Source Han Serif Latin)")

from news_bot.processing.image_generator import generate_image_from_article  # noqa: E402

# ── Config ─────────────────────────────────────────────────────────────

INPUT_FILE  = "articles_output.json"
OUTPUT_DIR  = "wechat_images"

SCHOOL_BRAND_MAP = {
    "NYU":       "#57068c",
    "USC":       "#990000",
    "EMORY":     "#222c66",
    "UCD":       "#022851",
    "UC DAVIS":  "#022851",
    "UBC":       "#002145",
    "EDINBURGH": "#041e42",
}

SCHOOL_FOLDERS = {
    "NYU":       "NYU_Weekly",
    "USC":       "USC_Weekly",
    "EMORY":     "EMORY_Weekly",
    "UCD":       "UCD_Weekly",
    "UC DAVIS":  "UCD_Weekly",
    "UBC":       "UBC_Weekly",
    "EDINBURGH": "EDIN_Weekly",
}


# ── Helpers ────────────────────────────────────────────────────────────

def detect_school(articles: list) -> str:
    """Infer school from sender / subject of the first few articles."""
    for article in articles[:5]:
        sender  = article.get("sender",  "").lower()
        subject = article.get("subject", "").lower()
        combined = sender + " " + subject

        if "nyu" in combined:
            return "NYU"
        if "usc" in combined or "annenberg" in combined:
            return "USC"
        if "emory" in combined:
            return "EMORY"
        if "ucdavis" in combined or "theaggie" in combined:
            return "UCD"
        if "ubc" in combined or "ubyssey" in combined:
            return "UBC"
        if "edinburgh" in combined or "ed.ac.uk" in combined:
            return "EDINBURGH"

    return "NYU"  # default


def generate_images(
    input_file: str = INPUT_FILE,
    output_base_dir: str = OUTPUT_DIR,
    school_override: str = None,
    page_width: int = 540,
    device_scale: int = 4,
    title_size: float = 22.5,
    body_size: float = 22.5,
) -> dict:
    """
    Convert articles_output.json → WeChat-style PNG images.

    Args:
        input_file:       Path to articles_output.json
        output_base_dir:  Root output directory
        school_override:  Force a specific school (NYU, USC, …)
        page_width:       Image width in pixels
        device_scale:     Higher = sharper (default 4)
        title_size:       Title font size
        body_size:        Body font size

    Returns:
        dict with success flag and list of generated file paths
    """
    # Load JSON
    if not os.path.exists(input_file):
        return {"success": False, "error": f"{input_file} not found"}

    with open(input_file, encoding="utf-8") as f:
        articles = json.load(f)

    if not articles:
        return {"success": False, "error": "No articles found in JSON"}

    # Detect / set school
    school = (school_override or detect_school(articles)).upper().strip()
    brand_color  = SCHOOL_BRAND_MAP.get(school, "#57068c")
    school_folder = SCHOOL_FOLDERS.get(school, "Generic_Weekly")
    is_ucd = school in ("UCD", "UC DAVIS")

    # Create output directory
    out_dir = Path(output_base_dir) / school_folder
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"[i] School  : {school}  (brand {brand_color})")
    print(f"[i] Articles: {len(articles)}")
    print(f"[i] Output  : {out_dir}")
    print()

    generated = []

    for idx, article in enumerate(articles, 1):
        title   = article.get("title", "").strip()
        body    = article.get("body",  "").strip()
        subject = article.get("subject", "")

        # Skip articles without translated content
        if not title or not body:
            print(f"  [{idx}] ⚠️  Skipping (no title/body): {subject[:60]}")
            continue

        # UCD alternates navy / gold sidebar
        left_bar_color = None
        if is_ucd:
            left_bar_color = "#022851" if (idx % 2 == 1) else "#FFBF00"

        # Safe filename from title
        safe_title = title[:40].replace("/", "_").replace("\\", "_").replace(":", "_")
        out_path = out_dir / f"{idx:02d}_{safe_title}.png"

        print(f"  [{idx}/{len(articles)}] {title[:55]}")

        try:
            generate_image_from_article(
                title=title,
                content=body,
                output_path=str(out_path),
                credits="",
                cover_image="",
                cover_caption="",
                page_width=page_width,
                device_scale=device_scale,
                title_size=title_size,
                body_size=body_size,
                brand_color=brand_color,
                left_bar_color=left_bar_color,
            )
            generated.append(str(out_path))
            print(f"         ✅ {out_path.name}")
        except Exception as e:
            print(f"         ❌ Failed: {e}")

    print(f"\n[✓] Done — {len(generated)} images saved to {out_dir}")
    return {
        "success": True,
        "school": school,
        "output_dir": str(out_dir),
        "generated_files": generated,
        "total": len(generated),
    }


# ── CLI ────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Convert articles_output.json → WeChat PNG images",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Schools:
  NYU       Purple  #57068c
  USC       Red     #990000
  EMORY     Blue    #222c66
  UCD       Navy    #022851  (alternates gold sidebar)
  UBC       Navy    #002145
  EDINBURGH Dark    #041e42
        """,
    )
    parser.add_argument("--input",  "-i", default=INPUT_FILE,
                        help=f"Input JSON file (default: {INPUT_FILE})")
    parser.add_argument("--out",    "-o", default=OUTPUT_DIR,
                        help=f"Output base directory (default: {OUTPUT_DIR})")
    parser.add_argument("--school", "-s",
                        help="Override school detection (NYU, USC, EMORY, UCD, UBC, EDINBURGH)")
    parser.add_argument("--page-width",   type=int,   default=540)
    parser.add_argument("--device-scale", type=int,   default=4)
    parser.add_argument("--title-size",   type=float, default=22.5)
    parser.add_argument("--body-size",    type=float, default=22.5)

    args = parser.parse_args()

    result = generate_images(
        input_file=args.input,
        output_base_dir=args.out,
        school_override=args.school,
        page_width=args.page_width,
        device_scale=args.device_scale,
        title_size=args.title_size,
        body_size=args.body_size,
    )

    if not result["success"]:
        print(f"[!] Error: {result['error']}")
        sys.exit(1)


if __name__ == "__main__":
    main()
