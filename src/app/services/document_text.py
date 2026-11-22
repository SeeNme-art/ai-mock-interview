"""Extract local, text-based material without retaining the uploaded file."""
from io import BytesIO
import logging
from typing import Literal
from xml.etree.ElementTree import ParseError
from zipfile import BadZipFile, ZipFile
from zlib import error as CompressionError

from defusedxml.ElementTree import fromstring
from defusedxml.common import DefusedXmlException
from pypdf import PdfReader, apply_configuration
from pypdf.errors import PyPdfError

from app.services.materials import TEXT_LIMIT, preview_text

DocumentFormat = Literal["pdf", "docx"]
MAX_DOCUMENT_BYTES = 5 * 1024 * 1024
MAX_PDF_PAGES = 20
MAX_PART_BYTES = 2 * 1024 * 1024
MAX_PDF_STREAM_BYTES = 4 * 1024 * 1024
WORD_NAMESPACES = ("http://schemas.openxmlformats.org/wordprocessingml/2006/main",
                   "http://purl.oclc.org/ooxml/wordprocessingml/main")

# The library can include input fragments in diagnostics. Only expose our fixed errors.
_pdf_logger = logging.getLogger("pypdf")
_pdf_logger.addHandler(logging.NullHandler())
_pdf_logger.propagate = False


class DocumentError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def document_too_large() -> DocumentError:
    return DocumentError("DOCUMENT_TOO_LARGE", "文件最大 5 MiB，请缩小文件或粘贴文本。")


def _pdf(content: bytes) -> str:
    if not content.startswith(b"%PDF-"):
        raise DocumentError("DOCUMENT_INVALID", "文件不是有效的 PDF，请核对格式或粘贴文本。")
    reader = PdfReader(BytesIO(content), strict=True)
    if reader.is_encrypted:
        raise DocumentError("DOCUMENT_ENCRYPTED", "请先在本机解除 PDF 密码，或粘贴文本。")
    if len(reader.pages) > MAX_PDF_PAGES:
        raise DocumentError("DOCUMENT_LIMIT", f"PDF 最多 {MAX_PDF_PAGES} 页，请拆分后上传。")
    texts = []
    chars = 0
    for page in reader.pages:
        stream = page.get_contents()
        if stream is not None and len(stream.get_data()) > MAX_PDF_STREAM_BYTES:
            raise DocumentError("DOCUMENT_LIMIT", "PDF 单页内容过大，请导出较简单的文档或粘贴文本。")
        part = page.extract_text().strip()
        if not part:
            continue
        chars += len(part) + bool(texts)
        if chars > TEXT_LIMIT:
            raise DocumentError("DOCUMENT_LIMIT", f"提取文本最多 {TEXT_LIMIT} 字符，请分段上传。")
        texts.append(part)
    return "\n".join(texts)


def _docx(content: bytes) -> str:
    if content.startswith(bytes.fromhex("D0CF11E0A1B11AE1")):
        raise DocumentError("DOCUMENT_INVALID", "请确认文件是未加密的 .docx，或粘贴文本。")
    with ZipFile(BytesIO(content)) as package:
        if "[Content_Types].xml" not in package.namelist():
            raise DocumentError("DOCUMENT_INVALID", "缺少 DOCX 文档结构，请另存为 .docx。")
        part = package.getinfo("word/document.xml")
        if part.flag_bits & 1:
            raise DocumentError("DOCUMENT_ENCRYPTED", "请先在本机解除 DOCX 密码，或粘贴文本。")
        if part.file_size > MAX_PART_BYTES:
            raise DocumentError("DOCUMENT_LIMIT", "DOCX 正文结构过大，请分成更短的文档。")
        with package.open(part) as stream:
            xml = stream.read(MAX_PART_BYTES + 1)
    if len(xml) > MAX_PART_BYTES:
        raise DocumentError("DOCUMENT_LIMIT", "DOCX 正文结构过大，请分成更短的文档。")
    root = fromstring(xml, forbid_dtd=True)
    for namespace in WORD_NAMESPACES:
        prefix = "{" + namespace + "}"
        if root.tag == prefix + "document":
            body = root.find(prefix + "body")
            if body is not None:
                return "\n".join("".join(node.text or "" for node in paragraph.iter(prefix + "t"))
                                 for paragraph in body.iter(prefix + "p"))
    raise DocumentError("DOCUMENT_INVALID", "缺少 DOCX 正文，请检查文件或粘贴文本。")


def extract_document(content: bytes, document_format: DocumentFormat) -> str:
    if len(content) > MAX_DOCUMENT_BYTES:
        raise document_too_large()
    try:
        if document_format == "pdf":
            with apply_configuration(
                array_based_stream_maximum_output_length=MAX_PDF_STREAM_BYTES,
                brotli_maximum_output_length=MAX_PDF_STREAM_BYTES,
                lzw_maximum_output_length=MAX_PDF_STREAM_BYTES,
                run_length_maximum_output_length=MAX_PDF_STREAM_BYTES,
                zlib_maximum_output_length=MAX_PDF_STREAM_BYTES,
                stream_decoding_work_maximum_length=4 * MAX_PDF_STREAM_BYTES,
                stream_parsing_work_maximum_count=100_000,
                page_tree_maximum_entries=200,
                xform_maximum_invocations_per_extraction=100,
                jbig2dec_binary=None,
            ):
                raw = _pdf(content)
        else:
            raw = _docx(content)
    except DocumentError:
        raise
    except (PyPdfError, BadZipFile, CompressionError, ParseError, DefusedXmlException,
            KeyError, ValueError, TypeError, IndexError, OverflowError, RecursionError, NotImplementedError):
        raise DocumentError("DOCUMENT_INVALID", "文件无法读取，请重新导出或改用粘贴文本。") from None
    if len(raw) > TEXT_LIMIT:
        raise DocumentError("DOCUMENT_LIMIT", f"提取文本最多 {TEXT_LIMIT} 字符，请分段上传。")
    text = preview_text(raw)
    if len(text) > TEXT_LIMIT:
        raise DocumentError("DOCUMENT_LIMIT", f"提取文本最多 {TEXT_LIMIT} 字符，请分段上传。")
    if not any(character.isalnum() for character in text):
        raise DocumentError("DOCUMENT_NO_TEXT", "没有可读文字。扫描件或图片请先转成文字，再粘贴到页面。")
    return text
