"""Build the offline annotation MVP: python build_annotation.py."""
import hashlib
import json
from pathlib import Path

from build_review import make_blocks

ROOT = Path(__file__).resolve().parent


def manuscript_blocks(text, source="manuscript/draft.md"):
    blocks = make_blocks(text, "md", source)
    occurrences = {}
    for block in blocks:
        # Content-based IDs survive unrelated insertion and reordering.
        digest = hashlib.sha256((source + "\0" + block["text"]).encode()).hexdigest()[:20]
        occurrences[digest] = occurrences.get(digest, 0) + 1
        block["id"] = f"p-{digest}-{occurrences[digest]}"
    return blocks


def build():
    source = "manuscript/draft.md"
    blocks = manuscript_blocks((ROOT / source).read_text(encoding="utf-8"), source)
    data = json.dumps({"source": source, "blocks": blocks}, ensure_ascii=False).replace("<", "\\u003c")
    template = (ROOT / "annotation/index.html").read_text(encoding="utf-8")
    page = template.replace("/*__STYLE__*/", (ROOT / "annotation/style.css").read_text())
    page = page.replace("/*__SCRIPT__*/", (ROOT / "annotation/app.js").read_text())
    page = page.replace("__MANUSCRIPT_DATA__", data)
    (ROOT / "annotation.html").write_text(page, encoding="utf-8")
    print(f"Built annotation.html ({len(blocks)} manuscript blocks)")


if __name__ == "__main__":
    build()
