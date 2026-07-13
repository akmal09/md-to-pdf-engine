"""Convert a Markdown file to PDF, rendering ```mermaid blocks as images.

Usage:
    python md_to_pdf.py <input.md> [output.pdf]

Uses `markdown` to render HTML and `xhtml2pdf` to produce the PDF.
Mermaid diagrams are rendered to PNG locally: the vendored Mermaid
bundle (vendor/mermaid-10.9.6.min.js) runs inside headless Chromium
via Playwright, so diagram content never leaves this machine. Images
are cached in <input>_diagrams/ next to the markdown file, so repeated
runs don't re-render unchanged diagrams.

One-time setup:
    python -m pip install playwright
    python -m playwright install chromium
"""

import atexit
import hashlib
import re
import sys
from pathlib import Path
from urllib.parse import unquote

import markdown
from xhtml2pdf import pisa


def _patch_xhtml2pdf_fonts():
    """Work around an xhtml2pdf bug on Windows: @font-face copies the font
    into a NamedTemporaryFile that stays locked while open, so reportlab
    cannot reopen it. Hand reportlab the original local path instead."""
    from xhtml2pdf.files import pisaFileObject

    orig = pisaFileObject.getNamedFile

    def getNamedFile(self):
        uri = self.instance.get_uri()
        if uri and Path(str(uri)).is_file():
            return str(uri)
        return orig(self)

    pisaFileObject.getNamedFile = getNamedFile


_patch_xhtml2pdf_fonts()

MERMAID_JS = Path(__file__).resolve().parent / "vendor" / "mermaid-10.9.6.min.js"
MERMAID_BLOCK = re.compile(r"```mermaid[ \t]*\n(.*?)\n[ \t]*```", re.DOTALL)

# xhtml2pdf's built-in Type1 fonts have no emoji glyphs, so emoji come out
# as black boxes. Wrap emoji runs in a span using a registered emoji font.
EMOJI_FONT_CANDIDATES = [
    Path("C:/Windows/Fonts/seguiemj.ttf"),          # Windows: Segoe UI Emoji
    Path("/usr/share/fonts/truetype/noto/NotoEmoji-Regular.ttf"),  # Linux
]
EMOJI_RUN = re.compile(
    "["
    "\\U0001F000-\\U0001FAFF"  # emoji, pictographs, transport, supplemental
    "\\u2300-\\u23FF"          # misc technical (watch, hourglass, ...)
    "\\u2600-\\u27BF"          # misc symbols & dingbats
    "\\u2B00-\\u2BFF"          # misc symbols & arrows (stars, ...)
    "\\uFE0F"                  # variation selector riding on the emoji
    "]+"
)

# A4 minus margins is ~493pt; xhtml2pdf treats 1px as 0.75pt, so cap at ~650px
MAX_IMG_WIDTH_PX = 650

CSS = """
@page {
    size: A4;
    margin: 2cm 1.8cm;
}
body {
    font-family: Helvetica, Arial, sans-serif;
    font-size: 10pt;
    line-height: 1.5;
    color: #1a1a1a;
}
h1 {
    font-size: 17pt;
    color: #0f2a4a;
    border-bottom: 2px solid #0f2a4a;
    padding-bottom: 4px;
    margin-top: 20px;
}
h2 {
    font-size: 13.5pt;
    color: #14406e;
    margin-top: 16px;
}
h3 {
    font-size: 11.5pt;
    color: #1a5291;
    margin-top: 12px;
}
h4 {
    font-size: 10.5pt;
    color: #1a5291;
}
p, li {
    text-align: justify;
}
table {
    width: 100%;
    border-collapse: collapse;
    margin: 8px 0;
    font-size: 8.5pt;
}
th {
    background-color: #0f2a4a;
    color: #ffffff;
    padding: 5px;
    border: 1px solid #444444;
    text-align: left;
}
td {
    padding: 5px;
    border: 1px solid #888888;
    vertical-align: top;
}
code {
    font-family: Courier, monospace;
    font-size: 8.5pt;
    color: #b03050;
    background-color: #f4f4f4;
}
pre {
    font-family: Courier, monospace;
    font-size: 8pt;
    background-color: #f4f4f4;
    border: 1px solid #dddddd;
    padding: 8px;
    margin: 8px 0;
}
blockquote {
    border-left: 3px solid #1a5291;
    padding-left: 10px;
    margin-left: 0;
    color: #444444;
    background-color: #f0f5fa;
}
a {
    color: #1a5291;
}
hr {
    border: 0;
    border-top: 1px solid #cccccc;
}
.diagram {
    margin: 10px 0;
    text-align: center;
}
"""


# One headless browser shared across all diagrams in a run.
_renderer = {}

# Runs in the page: render to SVG, then pin the element to its natural
# width (mermaid emits width:100%/max-width for responsive embedding,
# which would otherwise size the screenshot to the viewport).
_RENDER_JS = """
async (code) => {
    const container = document.getElementById('container');
    container.innerHTML = '';
    window.__n = (window.__n || 0) + 1;
    const { svg } = await mermaid.render('d' + window.__n, code);
    container.innerHTML = svg;
    const el = container.querySelector('svg');
    const maxW = parseFloat(el.style.maxWidth);
    if (maxW) { el.style.width = maxW + 'px'; el.style.maxWidth = 'none'; }
    el.style.backgroundColor = '#ffffff';
}
"""


def _get_page():
    if "page" in _renderer:
        return _renderer["page"]
    if not MERMAID_JS.is_file():
        raise FileNotFoundError(f"vendored mermaid bundle not found: {MERMAID_JS}")

    from playwright.sync_api import sync_playwright

    pw = sync_playwright().start()
    browser = pw.chromium.launch()
    # device_scale_factor=2 renders at 2x for crisp PDF output; the
    # display width is halved again in render_mermaid_blocks().
    page = browser.new_page(
        viewport={"width": 1600, "height": 1200}, device_scale_factor=2
    )
    # hard guarantee that no diagram content leaves the machine: the page
    # is built from local strings/files, and any network request is aborted
    page.route("**/*", lambda route: route.abort())
    page.set_content('<body style="margin:0;background:#ffffff">'
                     '<div id="container"></div></body>')
    page.add_script_tag(path=str(MERMAID_JS))
    page.evaluate("() => mermaid.initialize({ startOnLoad: false, theme: 'default' })")

    _renderer.update(pw=pw, browser=browser, page=page)
    atexit.register(_close_renderer)
    return page


def _close_renderer():
    if _renderer:
        _renderer["browser"].close()
        _renderer["pw"].stop()
        _renderer.clear()


def fetch_png(code: str) -> bytes:
    """Render mermaid code to PNG in a local headless browser (no network)."""
    page = _get_page()
    page.evaluate(_RENDER_JS, code)
    return page.locator("#container svg").screenshot(type="png")


def png_width(data: bytes) -> int:
    """Read the pixel width from a PNG header (IHDR is always first)."""
    return int.from_bytes(data[16:20], "big")


def render_mermaid_blocks(md_text: str, img_dir: Path) -> str:
    """Replace each ```mermaid block with an <img> tag pointing to a rendered PNG."""

    def replace(match: re.Match) -> str:
        code = match.group(1).strip()
        digest = hashlib.sha256(code.encode("utf-8")).hexdigest()[:16]
        img_path = img_dir / f"mermaid_{digest}.png"

        if not img_path.is_file():
            try:
                data = fetch_png(code)
            except Exception as exc:
                print(f"WARNING: mermaid render failed ({exc}); keeping code block")
                return match.group(0)
            img_dir.mkdir(exist_ok=True)
            img_path.write_bytes(data)
            print(f"Rendered diagram -> {img_path.name}")

        # scale=2 doubles the pixels, so halve for display; cap to page width
        width = min(png_width(img_path.read_bytes()) // 2, MAX_IMG_WIDTH_PX)
        # blank lines around the tag keep markdown from mangling it
        return f'\n<div class="diagram"><img src="{img_path.as_posix()}" width="{width}"></div>\n'

    return MERMAID_BLOCK.sub(replace, md_text)


def find_emoji_font() -> Path | None:
    for font in EMOJI_FONT_CANDIDATES:
        if font.is_file():
            return font
    return None


def wrap_emoji(html: str, font: Path | None) -> str:
    """Wrap emoji runs in a span styled with the emoji font.

    Only characters the emoji font actually covers are wrapped; the rest
    stay in the base font (e.g. ★ has a glyph there but not in Segoe UI
    Emoji). U+FE0F presentation selectors are dropped — they occupy a
    blank glyph of their own in the PDF.
    """
    if font is None:
        print("WARNING: no emoji font found; emoji may render as boxes")
        return html

    from reportlab.pdfbase.ttfonts import TTFontFile

    covered = set(TTFontFile(str(font)).charToGlyph)

    def replace(match: re.Match) -> str:
        out = []
        for ch in match.group(0):
            if ord(ch) == 0xFE0F:
                continue
            if ord(ch) in covered:
                out.append(f'<span class="emoji">{ch}</span>')
            else:
                out.append(ch)
        return "".join(out)

    return EMOJI_RUN.sub(replace, html)


def make_link_callback(base_dir: Path):
    """Resolve image srcs relative to the markdown file, not the CWD."""

    def link_callback(uri: str, rel: str) -> str:
        if uri.startswith(("http://", "https://", "data:")):
            return uri
        # markdown URLs encode spaces as %20; decode before touching the fs
        path = Path(unquote(uri))
        if not path.is_absolute():
            path = base_dir / path
        if not path.is_file():
            print(f"WARNING: image not found: {uri}")
        return str(path)

    return link_callback


def convert(md_path: Path, pdf_path: Path) -> bool:
    md_path = md_path.resolve()
    md_text = md_path.read_text(encoding="utf-8")
    md_text = render_mermaid_blocks(md_text, md_path.parent / f"{md_path.stem}_diagrams")
    body = markdown.markdown(
        md_text,
        extensions=["tables", "fenced_code", "sane_lists", "toc", "nl2br"],
    )

    css = CSS
    emoji_font = find_emoji_font()
    body = wrap_emoji(body, emoji_font)
    if emoji_font:
        css += (
            f'@font-face {{ font-family: "emoji"; src: url("{emoji_font.as_posix()}"); }}\n'
            '.emoji { font-family: "emoji"; }\n'
        )

    html = f"<html><head><style>{css}</style></head><body>{body}</body></html>"

    with pdf_path.open("wb") as f:
        result = pisa.CreatePDF(
            html, dest=f, encoding="utf-8",
            link_callback=make_link_callback(md_path.parent),
        )
    return not result.err


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1

    md_path = Path(sys.argv[1])
    if not md_path.is_file():
        print(f"Input file not found: {md_path}")
        return 1

    pdf_path = Path(sys.argv[2]) if len(sys.argv) > 2 else md_path.with_suffix(".pdf")
    if convert(md_path, pdf_path):
        print(f"OK: {pdf_path}")
        return 0
    print("Conversion failed.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
