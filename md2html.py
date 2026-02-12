#!/usr/bin/env python3
"""Convert Markdown files to self-contained HTML with RTL/LTR detection and embedded images."""

import argparse
import base64
import mimetypes
import os
import re
import sys
import unicodedata
from pathlib import Path

import markdown
import requests
from bs4 import BeautifulSoup

EMBEDDED_CSS = """\
* {
    margin: 0;
    padding: 0;
    box-sizing: border-box;
}
body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Oxygen,
        Ubuntu, Cantarell, "Fira Sans", "Droid Sans", "Helvetica Neue", Arial,
        sans-serif;
    line-height: 1.7;
    color: #1a1a1a;
    background: #fff;
    max-width: 48em;
    margin: 0 auto;
    padding: 2em 1.5em;
}
h1, h2, h3, h4, h5, h6 {
    margin-top: 1.4em;
    margin-bottom: 0.6em;
    font-weight: 600;
    line-height: 1.3;
}
h1 { font-size: 2em; border-bottom: 1px solid #e0e0e0; padding-bottom: 0.3em; }
h2 { font-size: 1.5em; border-bottom: 1px solid #e0e0e0; padding-bottom: 0.25em; }
h3 { font-size: 1.25em; }
p { margin-bottom: 1em; }
a { color: #0366d6; text-decoration: none; }
a:hover { text-decoration: underline; }
img { max-width: 100%; height: auto; display: block; margin: 1em 0; }
pre {
    background: #f6f8fa;
    border: 1px solid #e1e4e8;
    border-radius: 6px;
    padding: 1em;
    overflow-x: auto;
    margin-bottom: 1em;
    direction: ltr;
    text-align: left;
}
code {
    font-family: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, Courier, monospace;
    font-size: 0.9em;
    direction: ltr;
}
p code, li code {
    background: #f0f0f0;
    padding: 0.15em 0.4em;
    border-radius: 3px;
}
blockquote {
    border-left: 4px solid #dfe2e5;
    padding: 0.5em 1em;
    margin-bottom: 1em;
    color: #555;
    background: #fafafa;
}
[dir="rtl"] blockquote {
    border-left: none;
    border-right: 4px solid #dfe2e5;
}
ul, ol { margin-bottom: 1em; padding-left: 2em; }
[dir="rtl"] ul, [dir="rtl"] ol { padding-left: 0; padding-right: 2em; }
li { margin-bottom: 0.3em; }
table {
    border-collapse: collapse;
    width: 100%;
    margin-bottom: 1em;
    overflow-x: auto;
    display: block;
}
th, td {
    border: 1px solid #dfe2e5;
    padding: 0.6em 1em;
    text-align: start;
}
th { background: #f6f8fa; font-weight: 600; }
tr:nth-child(even) { background: #fafbfc; }
hr { border: none; border-top: 1px solid #e0e0e0; margin: 2em 0; }
"""


def preprocess_obsidian_syntax(text: str) -> str:
    """Convert Obsidian-style ![[image.ext]] and ![[image.ext|alt]] to standard Markdown."""
    def replace_wikilink_image(m):
        content = m.group(1)
        # ![[file.png|alt text]] or ![[file.png]]
        if "|" in content:
            filename, alt = content.split("|", 1)
        else:
            filename, alt = content, ""
        return f"![{alt}]({filename})"

    return re.sub(r"!\[\[([^\]]+)\]\]", replace_wikilink_image, text)


def find_image_in_ancestors(filename: str, base_dir: Path) -> Path | None:
    """Search for an image file in base_dir and ancestor directories up to the vault root."""
    current = base_dir.resolve()
    while True:
        # Check flat files in this directory first (fast)
        candidate = current / filename
        if candidate.is_file():
            return candidate
        # Then search subdirectories
        for match in current.rglob(filename):
            if match.is_file():
                return match
        # Stop at vault root (Obsidian marker) or filesystem root
        if (current / ".obsidian").is_dir() or (current / ".git").is_dir():
            break
        parent = current.parent
        if parent == current:
            break
        current = parent
    return None


def strip_markdown_and_html(text: str) -> str:
    """Remove Markdown syntax and HTML tags to get plain text for direction detection."""
    # Remove code blocks
    text = re.sub(r"```[\s\S]*?```", " ", text)
    text = re.sub(r"`[^`]+`", " ", text)
    # Remove images and links
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)
    text = re.sub(r"\[[^\]]*\]\([^)]*\)", " ", text)
    # Remove HTML tags
    text = re.sub(r"<[^>]+>", " ", text)
    # Remove Markdown formatting
    text = re.sub(r"[#*_~>`|\\-]+", " ", text)
    # Remove URLs
    text = re.sub(r"https?://\S+", " ", text)
    return text


def detect_text_direction(text: str) -> str:
    """Detect dominant text direction using Unicode bidi character properties."""
    clean = strip_markdown_and_html(text)
    rtl_count = 0
    ltr_count = 0
    for char in clean:
        bidi = unicodedata.bidirectional(char)
        if bidi in ("R", "AL", "AN"):
            rtl_count += 1
        elif bidi == "L":
            ltr_count += 1
    return "rtl" if rtl_count > ltr_count else "ltr"


def read_image_as_data_uri(src: str, base_dir: Path) -> str | None:
    """Convert an image source (local path or URL) to a data URI. Returns None on failure."""
    try:
        if src.startswith(("http://", "https://")):
            resp = requests.get(src, timeout=15)
            resp.raise_for_status()
            content_type = resp.headers.get("Content-Type", "").split(";")[0].strip()
            if not content_type:
                content_type = mimetypes.guess_type(src)[0] or "image/png"
            data = base64.b64encode(resp.content).decode("ascii")
            return f"data:{content_type};base64,{data}"
        else:
            # Local file — resolve relative to the Markdown file's directory
            path = Path(src) if Path(src).is_absolute() else base_dir / src
            path = path.resolve()
            if not path.is_file():
                # Obsidian-style: search by filename in ancestor directories
                found = find_image_in_ancestors(Path(src).name, base_dir)
                if found:
                    path = found
                else:
                    print(f"Warning: image not found: {src}", file=sys.stderr)
                    return None
            content_type = mimetypes.guess_type(str(path))[0] or "image/png"
            data = base64.b64encode(path.read_bytes()).decode("ascii")
            return f"data:{content_type};base64,{data}"
    except Exception as exc:
        print(f"Warning: could not embed image '{src}': {exc}", file=sys.stderr)
        return None


def embed_images(html: str, base_dir: Path) -> str:
    """Find all <img> tags in HTML and replace src with base64 data URIs."""
    soup = BeautifulSoup(html, "html.parser")
    for img in soup.find_all("img"):
        src = img.get("src")
        if not src or src.startswith("data:"):
            continue
        data_uri = read_image_as_data_uri(src, base_dir)
        if data_uri:
            img["src"] = data_uri
    return str(soup)


def convert_markdown_to_html(md_text: str, title: str, base_dir: Path, embed: bool = True) -> str:
    """Convert Markdown text to a complete, self-contained HTML document."""
    extensions = ["extra", "codehilite", "toc", "smarty", "sane_lists"]
    extension_configs = {
        "codehilite": {"css_class": "highlight", "guess_lang": True, "noclasses": True},
    }
    md_text = preprocess_obsidian_syntax(md_text)
    body_html = markdown.markdown(md_text, extensions=extensions, extension_configs=extension_configs)

    if embed:
        body_html = embed_images(body_html, base_dir)

    direction = detect_text_direction(md_text)
    lang = "ar" if direction == "rtl" else "en"

    return f"""\
<!DOCTYPE html>
<html lang="{lang}" dir="{direction}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
<style>
{EMBEDDED_CSS}</style>
</head>
<body>
<h1>{title}</h1>
{body_html}
</body>
</html>
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Convert Markdown to self-contained HTML.")
    parser.add_argument("input", help="Input Markdown file")
    parser.add_argument("-o", "--output", help="Output HTML file (default: <input>.html)")
    parser.add_argument(
        "--no-embed-images",
        action="store_true",
        help="Do not embed images as base64 data URIs",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    input_path = Path(args.input).resolve()
    if not input_path.is_file():
        print(f"Error: file not found: {input_path}", file=sys.stderr)
        sys.exit(1)

    md_text = input_path.read_text(encoding="utf-8")
    title = input_path.stem.replace("-", " ").replace("_", " ").title()

    output_path = Path(args.output) if args.output else input_path.with_suffix(".html")

    html = convert_markdown_to_html(
        md_text,
        title=title,
        base_dir=input_path.parent,
        embed=not args.no_embed_images,
    )

    output_path.write_text(html, encoding="utf-8")
    print(f"Wrote {output_path}")


if __name__ == "__main__":
    main()
