"""Regression coverage for changes specific to the independent release."""
from io import BytesIO
from pathlib import Path
import unittest

from docx import Document
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from afr_agent.intake import digest, units
from afr_agent.presentation import ARCHIVE_BOARD_LAYOUT, board


class IndependentReleaseCoreTests(unittest.TestCase):
    def test_word_title_and_source_paragraph_are_kept(self):
        document = Document()
        document.add_heading("合成研报：社区预警", 0)
        document.add_paragraph("演练中记录了第一次预警和封路决定。")
        output = BytesIO()
        document.save(output)
        data = output.getvalue()
        sha = digest(data)
        parts, _, _, _ = units(Path("export-01.docx"), data, sha, None)
        self.assertIn("社区预警", parts[0]["text"])
        self.assertIn("封路决定", parts[1]["text"])
        self.assertEqual(parts[1]["element"], f"{sha}:e2")

    def test_pdf_preserves_page_locator_and_reports_blank_page(self):
        writer = PdfWriter()
        page = writer.add_blank_page(300, 300)
        page[NameObject("/Resources")] = DictionaryObject({
            NameObject("/Font"): DictionaryObject({
                NameObject("/F1"): DictionaryObject({
                    NameObject("/Type"): NameObject("/Font"),
                    NameObject("/Subtype"): NameObject("/Type1"),
                    NameObject("/BaseFont"): NameObject("/Helvetica"),
                }),
            }),
        })
        stream = DecodedStreamObject()
        stream.set_data(b"BT /F1 14 Tf 30 200 Td (Synthetic flood bulletin) Tj ET")
        page.replace_contents(stream)
        writer.add_blank_page(300, 300)
        output = BytesIO()
        writer.write(output)
        data = output.getvalue()
        parts, gaps, _, _ = units(Path("sample.pdf"), data, digest(data), None)
        self.assertEqual(parts, [{"kind": "paragraph", "text": "Synthetic flood bulletin", "locator": {"page": 1}}])
        self.assertTrue(any("第 2 页需要 OCR" in gap for gap in gaps))

    def test_single_chapter_uses_linked_parallel_lanes_for_three_topics(self):
        evidence = {"folder": "资料库/合成研报", "title": "合成研报", "suffix": ".md", "gaps": [],
                    "parts": [{"kind": "paragraph"} for _ in range(3)]}
        topics = [{"branch": "响应流程", "title": title, "summary": "演练的可核对发现。", "start": i, "end": i}
                  for i, title in enumerate(("预警", "派单", "行动"), 1)]
        layout = board(evidence, topics, layout=ARCHIVE_BOARD_LAYOUT)
        cards = {node["id"]: node for node in layout["nodes"]}
        self.assertEqual(cards["t1"]["x"], cards["t2"]["x"])
        self.assertGreater(cards["t3"]["x"], cards["t2"]["x"])
        self.assertIn("完整阅读.md#^topic-3", cards["t3"]["text"])
        self.assertIn("固定原文.md#^e3", cards["t3"]["text"])
