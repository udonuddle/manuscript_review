import unittest
from build_annotation import manuscript_blocks


class ParagraphIds(unittest.TestCase):
    def test_stable_across_insertions_and_moves(self):
        old = manuscript_blocks("# Title\n\n## One\n\nFirst.\n\nSecond.")
        new = manuscript_blocks("# Title\n\nNew.\n\n## One\n\nSecond.\n\nFirst.")
        ids = {b["text"]: b["id"] for b in new}
        for block in old:
            self.assertEqual(block["id"], ids[block["text"]])

    def test_duplicate_paragraphs_have_unique_ids(self):
        blocks = manuscript_blocks("Same.\n\nSame.")
        self.assertEqual(len({b["id"] for b in blocks}), 2)
        self.assertEqual(blocks, manuscript_blocks("Same.\n\nSame."))

    def test_section_and_structured_blocks(self):
        blocks = manuscript_blocks("# Title\n\n## Method\n\nA paragraph.\n\n$$\nx = 1\n\n+y\n$$\n\n```\ncode\n\nmore\n```")
        self.assertEqual([b["t"] for b in blocks], ["heading", "heading", "para", "math", "code"])
        self.assertEqual(blocks[2]["sec"], "Method")


if __name__ == "__main__":
    unittest.main()
