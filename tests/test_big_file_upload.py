import io
import tempfile
import unittest
import zipfile
from pathlib import Path

from fastapi.testclient import TestClient

from app import auth, db, file_text
from app.main import TEXT_ATTACHMENT_CHARS, _text_attachment_block, _uploaded_text_attachments, app


def make_pdf(text: str) -> bytes:
    """手搓一份一页、带一行字的 PDF，省得为测试再装一个写 PDF 的库。"""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(out.tell())
        out.write(f"{number} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets:
        out.write(f"{offset:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return out.getvalue()


def make_zip(files: dict[str, str]) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        for path, body in files.items():
            archive.writestr(path, body)
    return out.getvalue()


W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
A = 'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
S = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'


class ExtractTextTest(unittest.TestCase):
    """模型只看得了文字：常见格式要读得出来，读不出来的要说清楚。"""

    def test_pdf(self):
        self.assertIn("Hello Dwell", file_text.extract_text("a.pdf", make_pdf("Hello Dwell")))

    def test_docx_keeps_paragraphs(self):
        doc = make_zip({"word/document.xml": f'<w:document {W}><w:body>'
                        '<w:p><w:r><w:t>第一段</w:t></w:r></w:p>'
                        '<w:p><w:r><w:t>第二</w:t></w:r><w:r><w:t>段</w:t></w:r></w:p>'
                        '</w:body></w:document>'})
        self.assertEqual(file_text.extract_text("笔记.docx", doc), "第一段\n第二段")

    def test_pptx_goes_slide_by_slide_in_order(self):
        slide = lambda words: f'<p:sld xmlns:p="x" {A}><a:p><a:r><a:t>{words}</a:t></a:r></a:p></p:sld>'
        deck = make_zip({"ppt/slides/slide10.xml": slide("最后"), "ppt/slides/slide2.xml": slide("开头")})
        text = file_text.extract_text("deck.pptx", deck)
        self.assertLess(text.index("开头"), text.index("最后"))

    def test_xlsx_resolves_shared_strings(self):
        book = make_zip({
            "xl/sharedStrings.xml": f'<sst {S}><si><t>名字</t></si><si><t>小猫</t></si></sst>',
            "xl/worksheets/sheet1.xml": f'<worksheet {S}><sheetData>'
                                        '<row><c t="s"><v>0</v></c><c><v>3</v></c></row>'
                                        '<row><c t="s"><v>1</v></c><c t="inlineStr"><is><t>在</t></is></c></row>'
                                        '</sheetData></worksheet>',
        })
        self.assertIn("名字\t3\n小猫\t在", file_text.extract_text("表.xlsx", book))

    def test_gbk_text(self):
        self.assertEqual(file_text.extract_text("旧.txt", "你好，世界".encode("gbk")), "你好，世界")

    def test_unreadable_kinds_explain_themselves(self):
        with self.assertRaisesRegex(ValueError, "docx"):
            file_text.extract_text("老.doc", b"\xd0\xcf\x11\xe0")
        with self.assertRaisesRegex(ValueError, "音频"):
            file_text.extract_text("歌.mp3", b"ID3")
        with self.assertRaisesRegex(ValueError, "读不出文字"):
            file_text.extract_text("x.bin", b"\x00\x01\x02")
        with self.assertRaisesRegex(ValueError, "扫描件"):
            file_text.extract_text("scan.pdf", make_pdf(""))


class UploadFlowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.previous_path = db.DB_PATH
        db.DB_PATH = str(Path(cls.tmp.name) / "upload.db")
        db.init_db()
        app.dependency_overrides[auth.require_auth] = lambda: None
        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        app.dependency_overrides.pop(auth.require_auth, None)
        db.DB_PATH = cls.previous_path
        cls.tmp.cleanup()

    def upload(self, name: str, data: bytes, chunk: int):
        pieces = [data[i:i + chunk] for i in range(0, len(data), chunk)] or [b""]
        upload_id = ""
        for index, piece in enumerate(pieces):
            done = int(index == len(pieces) - 1)
            response = self.client.post(
                "/api/upload", params={"name": name, "idx": index, "done": done, "id": upload_id},
                content=piece,
            )
            if response.status_code != 200:
                return response
            upload_id = response.json()["id"]
        return response

    def test_chunks_are_joined_read_and_sent_once(self):
        body = ("第一行\n" + "长长的正文。" * 50_000).encode()
        response = self.upload("大笔记.md", body, chunk=64 * 1024)
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertTrue(result["truncated"])
        self.assertEqual(result["chars"], TEXT_ATTACHMENT_CHARS)

        files = _uploaded_text_attachments([{"kind": "upload", "id": result["id"], "name": "大笔记.md"}])
        self.assertEqual(files[0]["name"], "大笔记.md")
        self.assertTrue(files[0]["text"].startswith("第一行"))
        self.assertIn("只放了开头", _text_attachment_block(files))
        # 只进这一轮：取过一次就没了
        self.assertEqual(_uploaded_text_attachments([{"kind": "upload", "id": result["id"]}]), [])

    def test_a_pdf_upload_is_read_into_text(self):
        response = self.upload("paper.pdf", make_pdf("Upload works"), chunk=100)
        self.assertEqual(response.status_code, 200, response.text)
        files = _uploaded_text_attachments([{"kind": "upload", "id": response.json()["id"]}])
        self.assertIn("Upload works", files[0]["text"])

    def test_unreadable_file_is_refused_with_a_reason(self):
        response = self.upload("歌.mp3", b"ID3" * 10, chunk=1024)
        self.assertEqual(response.status_code, 400)
        self.assertIn("音频", response.json()["detail"])

    def test_chunks_out_of_order_or_unknown_are_refused(self):
        first = self.client.post("/api/upload", params={"name": "a.txt", "idx": 0, "done": 0}, content=b"abc")
        upload_id = first.json()["id"]
        skipped = self.client.post("/api/upload", params={"name": "a.txt", "idx": 2, "done": 1, "id": upload_id},
                                   content=b"def")
        self.assertEqual(skipped.status_code, 409)
        unknown = self.client.post("/api/upload", params={"name": "a.txt", "idx": 1, "done": 1, "id": "nope"},
                                   content=b"def")
        self.assertEqual(unknown.status_code, 404)


if __name__ == "__main__":
    unittest.main()
