"""
测试 convert.py：_is_docm_file 与 detect_docm 合并后行为一致。
"""
import sys
import zipfile
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import convert  # noqa: E402


CONTENT_TYPES_MACRO = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Override PartName="/word/document.xml"
 ContentType="application/vnd.ms-word.document.macroEnabled.main+xml"/>
</Types>"""

CONTENT_TYPES_NORMAL = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Override PartName="/word/document.xml"
 ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>"""


def _make_fake_docx(path: Path, content_types_xml: str):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml", content_types_xml)


def test_macro_docx_detected_by_both_paths():
    with tempfile.TemporaryDirectory() as td:
        fp = Path(td) / "fake.docx"
        _make_fake_docx(fp, CONTENT_TYPES_MACRO)

        is_problem, reason = convert.detect_docm(fp)
        assert is_problem is True
        assert "宏文档" in reason

        assert convert._is_docm_file(fp) is True
    print("test_macro_docx_detected_by_both_paths OK")


def test_normal_docx_not_flagged():
    with tempfile.TemporaryDirectory() as td:
        fp = Path(td) / "normal.docx"
        _make_fake_docx(fp, CONTENT_TYPES_NORMAL)

        is_problem, reason = convert.detect_docm(fp)
        assert is_problem is False
        assert reason == ""

        assert convert._is_docm_file(fp) is False
    print("test_normal_docx_not_flagged OK")


def test_corrupt_zip_flagged_as_problem():
    with tempfile.TemporaryDirectory() as td:
        fp = Path(td) / "corrupt.docx"
        fp.write_bytes(b"not a real zip file")

        is_problem, reason = convert.detect_docm(fp)
        assert is_problem is True

        assert convert._is_docm_file(fp) is True
    print("test_corrupt_zip_flagged_as_problem OK")


def test_non_docx_extension_ignored():
    with tempfile.TemporaryDirectory() as td:
        fp = Path(td) / "some.pdf"
        fp.write_bytes(b"%PDF-1.4 fake")
        is_problem, reason = convert.detect_docm(fp)
        assert is_problem is False
        assert convert._is_docm_file(fp) is False
    print("test_non_docx_extension_ignored OK")


if __name__ == "__main__":
    test_macro_docx_detected_by_both_paths()
    test_normal_docx_not_flagged()
    test_corrupt_zip_flagged_as_problem()
    test_non_docx_extension_ignored()
    print("\n全部通过 ✅")
