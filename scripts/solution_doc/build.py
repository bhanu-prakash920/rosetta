"""Fill the provided Solution Document template with Rosetta's content.

    python scripts/solution_doc/build.py
    -> docs/Rosetta_Solution_Document.docx (and .pdf when LibreOffice is available)

Works on the template's own XML, so its styles, numbering, header and footer are
kept. Every number is read from docs/evidence at build time: the document cannot
drift from the measurements.
"""
from __future__ import annotations

import re
import shutil
import sys
import zipfile
from pathlib import Path

# escape() only escapes text we write; nothing is parsed, so there is no XXE.
from xml.sax.saxutils import escape  # nosemgrep: python.lang.security.use-defused-xml.use-defused-xml

from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "Motorq_Hackathon_Solution_Document_Template.docx"
OUT = ROOT / "docs" / "Rosetta_Solution_Document.docx"
EV = ROOT / "docs" / "evidence"
sys.path.insert(0, str(Path(__file__).parent))
from content import build_sections  # noqa: E402

W_CONTENT = 9306          # page width minus margins, in twentieths of a point

# The template tells participants what to write in each section. Those prompts are
# dropped: this document answers them. Bulleted prompts are all List Paragraph;
# these three are body text. "Video Link:" is a field to fill, not a prompt, so it stays.
GUIDANCE_PREFIXES = ("Brief overview", "Every feature must be traceable to code", "Videos longer than 5:00")
KEEP_FIELDS = ("Video Link:",)
EMU_PER_INCH = 914400


class Doc:
    def __init__(self) -> None:
        self.media: list[tuple[str, Path]] = []
        self.rid = 100
        self.pic = 1000

    # ------------------------------------------------------------------ text
    @staticmethod
    def runs(text: str, size: int | None = None, color: str | None = None, mono: bool = False) -> str:
        """**bold** and `code` inside plain text."""
        out = []
        for part in re.split(r"(\*\*[^*]+\*\*|`[^`]+`)", text):
            if not part:
                continue
            bold = part.startswith("**")
            code = part.startswith("`")
            t = part[2:-2] if bold else part[1:-1] if code else part
            rpr = ""
            if bold:
                rpr += "<w:b/><w:bCs/>"
            if code or mono:
                rpr += '<w:rFonts w:ascii="Consolas" w:hAnsi="Consolas" w:cs="Consolas"/>'
            if color:
                rpr += f'<w:color w:val="{color}"/>'
            if size or code or mono:
                sz = size or 18
                rpr += f'<w:sz w:val="{sz}"/><w:szCs w:val="{sz}"/>'
            out.append(f'<w:r>{"<w:rPr>" + rpr + "</w:rPr>" if rpr else ""}<w:t xml:space="preserve">{escape(t)}</w:t></w:r>')
        return "".join(out)

    def p(self, text: str, after: int = 140, keep: bool = False) -> str:
        k = "<w:keepNext/>" if keep else ""
        return f'<w:p><w:pPr>{k}<w:spacing w:after="{after}" w:line="290" w:lineRule="auto"/></w:pPr>{self.runs(text)}</w:p>'

    def sub(self, text: str) -> str:
        """A small bold lead-in heading inside a section."""
        return (f'<w:p><w:pPr><w:keepNext/><w:spacing w:before="160" w:after="60"/></w:pPr>'
                f'<w:r><w:rPr><w:b/><w:bCs/><w:color w:val="C2370C"/></w:rPr><w:t xml:space="preserve">{escape(text)}</w:t></w:r></w:p>')

    def bullet(self, text: str) -> str:
        return ('<w:p><w:pPr><w:pStyle w:val="ListParagraph"/><w:numPr><w:ilvl w:val="0"/><w:numId w:val="2"/></w:numPr>'
                f'<w:spacing w:after="70" w:line="280" w:lineRule="auto"/></w:pPr>{self.runs(text)}</w:p>')

    def code(self, text: str) -> str:
        paras = []
        for line in text.strip("\n").split("\n"):
            paras.append('<w:p><w:pPr><w:shd w:val="clear" w:color="auto" w:fill="F2F2F2"/><w:spacing w:after="0" w:line="240" w:lineRule="auto"/>'
                         f'<w:ind w:left="120"/></w:pPr>{self.runs(line or " ", size=16, mono=True)}</w:p>')
        return "".join(paras) + '<w:p><w:pPr><w:spacing w:after="80"/></w:pPr></w:p>'

    def caption(self, text: str) -> str:
        return (f'<w:p><w:pPr><w:spacing w:before="40" w:after="200"/><w:jc w:val="left"/></w:pPr>'
                f'<w:r><w:rPr><w:i/><w:iCs/><w:color w:val="595959"/><w:sz w:val="18"/><w:szCs w:val="18"/></w:rPr>'
                f'<w:t xml:space="preserve">{escape(text)}</w:t></w:r></w:p>')

    # ----------------------------------------------------------------- table
    def table(self, header: list[str], rows: list[list[str]], widths: list[float] | None = None, size: int = 17) -> str:
        n = len(header)
        widths = widths or [1] * n
        tot = sum(widths)
        cols = [int(W_CONTENT * w / tot) for w in widths]
        cols[-1] = W_CONTENT - sum(cols[:-1])
        border = "".join(f'<w:{s} w:val="single" w:sz="4" w:space="0" w:color="BFBFBF"/>' for s in ("top", "left", "bottom", "right"))

        def cell(text: str, w: int, head: bool) -> str:
            shd = '<w:shd w:val="clear" w:color="auto" w:fill="F2F2F2"/>' if head else ""
            paras = "".join(
                f'<w:p><w:pPr><w:keepLines/><w:spacing w:after="0" w:line="250" w:lineRule="auto"/></w:pPr>'
                f'{self.runs(("**" + line + "**") if head and line else line, size=size)}</w:p>'
                for line in str(text).split("\n"))
            return (f'<w:tc><w:tcPr><w:tcW w:w="{w}" w:type="dxa"/><w:tcBorders>{border}</w:tcBorders>{shd}'
                    '<w:tcMar><w:top w:w="60" w:type="dxa"/><w:left w:w="100" w:type="dxa"/><w:bottom w:w="60" w:type="dxa"/>'
                    f'<w:right w:w="100" w:type="dxa"/></w:tcMar></w:tcPr>{paras}</w:tc>')

        xml = (f'<w:tbl><w:tblPr><w:tblW w:w="{W_CONTENT}" w:type="dxa"/><w:tblLayout w:type="fixed"/>'
               '<w:tblCellMar><w:left w:w="10" w:type="dxa"/><w:right w:w="10" w:type="dxa"/></w:tblCellMar>'
               '<w:tblLook w:val="0000" w:firstRow="0" w:lastRow="0" w:firstColumn="0" w:lastColumn="0" w:noHBand="0" w:noVBand="0"/></w:tblPr>'
               '<w:tblGrid>' + "".join(f'<w:gridCol w:w="{c}"/>' for c in cols) + '</w:tblGrid>')
        xml += '<w:tr><w:trPr><w:tblHeader/><w:cantSplit/></w:trPr>' + "".join(cell(h, c, True) for h, c in zip(header, cols)) + "</w:tr>"
        for r in rows:
            xml += '<w:tr><w:trPr><w:cantSplit/></w:trPr>' + "".join(cell(v, c, False) for v, c in zip(r, cols)) + "</w:tr>"
        return xml + '</w:tbl><w:p><w:pPr><w:spacing w:after="120"/></w:pPr></w:p>'

    # ----------------------------------------------------------------- image
    def image(self, path: Path, width_in: float = 6.4, caption: str = "") -> str:
        path = Path(path)
        if not path.exists():
            return self.p(f"[missing image: {path.name}]")
        with Image.open(path) as im:
            w, h = im.size
            if h > w * 1.05 and "screenshots" in path.parts:
                # a full-page screenshot is unreadable at page width: keep its top
                crop = OUT.parent.parent / "data" / "doc_crops"
                crop.mkdir(parents=True, exist_ok=True)
                target = crop / path.name
                im.convert("RGB").crop((0, 0, w, int(w * 0.95))).save(target)
                path, h = target, int(w * 0.95)
        width_in = min(width_in, W_CONTENT / 1440)
        cx = int(width_in * EMU_PER_INCH)
        cy = int(cx * h / w)
        max_cy = int(8.4 * EMU_PER_INCH)
        if cy > max_cy:
            cx, cy = int(cx * max_cy / cy), max_cy
        self.rid += 1
        self.pic += 1
        rid = f"rIdImg{self.rid}"
        name = f"rosetta_{self.pic}{path.suffix.lower()}"
        self.media.append((rid, path, name))
        drawing = (
            f'<w:r><w:drawing><wp:inline distT="0" distB="0" distL="0" distR="0"><wp:extent cx="{cx}" cy="{cy}"/>'
            f'<wp:docPr id="{self.pic}" name="{escape(path.stem)}"/><wp:cNvGraphicFramePr>'
            '<a:graphicFrameLocks xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" noChangeAspect="1"/></wp:cNvGraphicFramePr>'
            '<a:graphic xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">'
            '<pic:pic xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture">'
            f'<pic:nvPicPr><pic:cNvPr id="{self.pic}" name="{name}"/><pic:cNvPicPr/></pic:nvPicPr>'
            f'<pic:blipFill><a:blip r:embed="{rid}"/><a:stretch><a:fillRect/></a:stretch></pic:blipFill>'
            f'<pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr>'
            '</pic:pic></a:graphicData></a:graphic></wp:inline></w:drawing></w:r>')
        xml = f'<w:p><w:pPr><w:keepNext/><w:spacing w:before="80" w:after="40"/><w:jc w:val="center"/></w:pPr>{drawing}</w:p>'
        return xml + (self.caption(caption) if caption else "")


def main() -> None:
    work = ROOT / "data" / "solution_doc_build"
    shutil.rmtree(work, ignore_errors=True)
    with zipfile.ZipFile(TEMPLATE) as z:
        z.extractall(work)
    for p in work.rglob("*"):
        if p.is_symlink():
            p.unlink()
    doc_path = work / "word" / "document.xml"
    xml = doc_path.read_text(encoding="utf-8")
    d = Doc()
    sections = build_sections(d, EV, ROOT)

    body_start = xml.index("<w:body>") + len("<w:body>")
    body_end = xml.index("<w:sectPr")
    body = xml[body_start:body_end]
    blocks = re.findall(r"<w:tbl>.*?</w:tbl>|<w:p[ >].*?</w:p>|<w:p/>", body, flags=re.S)

    def text_of(b: str) -> str:
        return "".join(re.findall(r"<w:t[^>]*>([^<]*)", b))

    out: list[str] = []
    current = None
    replaced_tables: set[str] = set()
    used = set()
    dropped: list[str] = []
    for b in blocks:
        t = text_of(b)
        style = re.search(r'<w:pStyle w:val="([^"]+)"', b)
        style = style.group(1) if style else ""
        if style in ("Heading1", "Heading2"):
            current = t.replace("&amp;", "&").strip()
            out.append(b)
            continue
        if current is not None and not t.strip().startswith(KEEP_FIELDS) \
                and (style == "ListParagraph" or t.strip().startswith(GUIDANCE_PREFIXES)):
            dropped.append(t.strip())
            continue
        # cover page fields
        for label, value in sections["cover"].items():
            if t.startswith(label) and value is not None:
                b = re.sub(r'(<w:r><w:rPr><w:color w:val="595959"/></w:rPr><w:t>)[^<]*(</w:t>)',
                           lambda m, v=value: m.group(1) + escape(v) + m.group(2), b, count=1)
        if b.startswith("<w:tbl>") and current in sections["tables"]:
            key = current
            n = sum(1 for k in replaced_tables if k.startswith(key + "#"))
            spec = sections["tables"][key]
            spec = spec[n] if isinstance(spec, list) else spec
            replaced_tables.add(f"{key}#{n}")
            out.append(spec)
            continue
        if t.strip() in ("[Write your answer here]", "[Insert diagrams and explanation]") and current in sections["answers"]:
            out.append(sections["answers"][current])
            used.add(current)
            continue
        if t.startswith("Video Link:") and sections.get("video") is not None:
            b = re.sub(r"\[URL\]", escape(sections["video"]), b)
        out.append(b)

    # sections whose template has no answer placeholder: insert before the next heading
    for sec, extra in sections.get("after", {}).items():
        for i, b in enumerate(out):
            if re.search(r'<w:pStyle w:val="Heading[12]"/>', b) and text_of(b).replace("&amp;", "&").strip() == sec:
                j = i + 1
                while j < len(out) and not re.search(r'<w:pStyle w:val="Heading[12]"/>', out[j]):
                    j += 1
                out.insert(j, extra)
                break
    missing = set(sections["answers"]) - used
    if missing:
        print("warning: no placeholder found for", sorted(missing))

    xml = xml[:body_start] + "".join(out) + xml[body_end:]
    # namespaces for inline pictures
    if 'xmlns:wp="' not in xml[:3000]:
        xml = xml.replace("<w:document ", '<w:document xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" ', 1)
    if 'xmlns:r="' not in xml[:3000]:
        xml = xml.replace("<w:document ", '<w:document xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" ', 1)
    doc_path.write_text(xml, encoding="utf-8")

    rels = work / "word" / "_rels" / "document.xml.rels"
    r = rels.read_text(encoding="utf-8")
    media = work / "word" / "media"
    media.mkdir(exist_ok=True)
    add = ""
    for rid, src, name in d.media:
        shutil.copy(src, media / name)
        add += (f'<Relationship Id="{rid}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" '
                f'Target="media/{name}"/>')
    rels.write_text(r.replace("</Relationships>", add + "</Relationships>"), encoding="utf-8")
    ct = work / "[Content_Types].xml"
    c = ct.read_text(encoding="utf-8")
    for ext, mime in (("png", "image/png"), ("jpg", "image/jpeg"), ("jpeg", "image/jpeg")):
        if f'Extension="{ext}"' not in c:
            c = c.replace("<Types ", "<Types ", 1).replace("</Types>", f'<Default Extension="{ext}" ContentType="{mime}"/></Types>')
    ct.write_text(c, encoding="utf-8")

    xml = doc_path.read_text(encoding="utf-8")
    xml = xml.replace(">Solution Document Template<", ">Solution Document: Rosetta<", 1)
    doc_path.write_text(xml, encoding="utf-8")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    if OUT.exists():
        OUT.unlink()
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(work.rglob("*")):
            if p.is_file():
                z.write(p, p.relative_to(work).as_posix())
    shutil.rmtree(work, ignore_errors=True)
    print(f"wrote {OUT.relative_to(ROOT)} with {len(d.media)} images, {len(dropped)} template prompts dropped")


if __name__ == "__main__":
    main()
