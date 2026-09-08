"""Is the downloaded PDF the paper we asked for?

The file-type checks cannot tell a cited document, a preview or a supplement from the paper.
These tests pin down what the identity check accepts, what it refuses, and -- the part that
matters most -- what it declines to judge.
"""
import zlib

import pytest

from paper_downloader.config.models import DownloadConfig
from paper_downloader.core.exceptions import WrongPaperError
from paper_downloader.download.downloader import PDFDownloader
from paper_downloader.download.identity import (
    NO_REFERENCE,
    TRUNCATED,
    UNREADABLE,
    VERIFIED,
    WRONG,
    ExpectedIdentity,
    expected_page_count,
    matched_fraction,
    squash,
    verify_pdf_identity,
)
from paper_downloader.pipeline.orchestrator import DownloadOrchestrator


# --- a PDF with real text, built by hand -------------------------------------------------

def _pdf(pages_text: list[str], *, title: str | None = None) -> bytes:
    """A minimal PDF whose pages carry the given text in Helvetica.

    Hand-built so the tests need no PDF writer: one content stream per page, one shared
    font, an optional /Title in the Info dictionary. pypdf extracts the text back exactly.
    """
    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    page_ids: list[int] = []
    pages_id = len(objects) + 2 * len(pages_text) + 1
    for text in pages_text:
        lines = text.split("\n")
        ops = ["BT", "/F1 11 Tf", "40 800 Td", "14 TL"]
        for line in lines:
            escaped = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            ops.append(f"({escaped}) Tj T*")
        ops.append("ET")
        stream = zlib.compress("\n".join(ops).encode("latin-1", errors="replace"))
        content = add(b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(stream) + stream + b"\nendstream")
        page = add(
            b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 595 842] /Contents %d 0 R "
            b"/Resources << /Font << /F1 %d 0 R >> >> >>" % (pages_id, content, font)
        )
        page_ids.append(page)
    kids = b" ".join(b"%d 0 R" % p for p in page_ids)
    pages = add(b"<< /Type /Pages /Kids [%s] /Count %d >>" % (kids, len(page_ids)))
    assert pages == pages_id
    catalog = add(b"<< /Type /Catalog /Pages %d 0 R >>" % pages)
    info = None
    if title is not None:
        info = add(b"<< /Title (" + title.encode("latin-1", errors="replace") + b") >>")

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    trailer = b"<< /Size %d /Root %d 0 R" % (len(objects) + 1, catalog)
    if info:
        trailer += b" /Info %d 0 R" % info
    trailer += b" >>"
    out += b"trailer\n" + trailer + b"\nstartxref\n%d\n%%%%EOF\n" % xref
    return bytes(out)


PAPER = "Attention Is All You Need"
FILLER = "\n".join(f"Line {i} of ordinary body text about transformers and attention heads." for i in range(30))


def _front(*lines: str) -> str:
    return "\n".join(lines) + "\n" + FILLER


# --- text normalisation ------------------------------------------------------------------

def test_squash_survives_line_breaks_hyphens_ligatures_and_entities():
    assert squash("Identiﬁcation of Sparse Auto-\nencoders") == squash("identification of sparse autoencoders")
    assert squash("salience &amp; framing") == squash("salience & framing")


def test_squash_keeps_non_latin_scripts():
    assert squash("Нейронные сети") != ""


def test_matched_fraction_tolerates_one_substituted_glyph():
    wanted = squash("V717F β-Amyloid Precursor Protein")
    on_page = squash("V717F b-Amyloid Precursor Protein")
    assert matched_fraction(wanted, on_page) > 0.9


def test_a_page_of_prose_does_not_supply_a_title_letter_by_letter():
    assert matched_fraction(squash(PAPER), squash(FILLER)) < 0.5


def test_page_ranges_that_could_mislead_are_declined():
    assert expected_page_count("107-122") == 16
    assert expected_page_count("1049-58") == 10
    assert expected_page_count("e12345") is None
    assert expected_page_count("S1-S8, S12") is None


# --- what is accepted --------------------------------------------------------------------

def test_the_doi_printed_on_the_paper_is_enough():
    pdf = _pdf([_front("Some retitled preprint", "https://doi.org/10.1000/xyz.123")])
    verdict = verify_pdf_identity(pdf, ExpectedIdentity(doi="10.1000/XYZ.123", title="Completely other title"))
    assert verdict.state == VERIFIED and verdict.signals == ["doi-in-text"]


def test_the_arxiv_stamp_is_enough_and_the_version_is_ignored():
    pdf = _pdf([_front("A New Title After Revision", "arXiv:2402.03175v3 [cs.CL] 1 Feb 2024")])
    verdict = verify_pdf_identity(pdf, ExpectedIdentity(arxiv_id="2402.03175v1", title="The Old Title"))
    assert verdict.state == VERIFIED and verdict.signals == ["arxiv-id-in-text"]


def test_a_datacite_arxiv_doi_yields_the_arxiv_id():
    """Semantic Scholar gives 10.48550/arXiv.* and no ArXiv id; arXiv prints the id, not the DOI."""
    assert ExpectedIdentity(doi="10.48550/arXiv.2402.03175").arxiv_id == "2402.03175"


def test_a_file_arxiv_served_for_the_id_is_that_paper_even_unstamped_and_retitled():
    pdf = _pdf([_front("From Passive to Persuasive: Localized Activation Injection")])
    expected = ExpectedIdentity(
        doi="10.48550/arXiv.2511.12832",
        title="From Passive to Persuasive: Steering Emotional Nuance in Human-AI Negotiation",
        source_url="https://arxiv.org/pdf/2511.12832v2",
    )
    verdict = verify_pdf_identity(pdf, expected)
    assert verdict.state == VERIFIED and verdict.signals == ["arxiv-id-in-url"]


def test_a_mirror_does_not_get_the_arxiv_url_credit():
    pdf = _pdf([_front("Something else entirely")])
    expected = ExpectedIdentity(arxiv_id="2511.12832", title=PAPER, source_url="https://mirror.example.org/pdf/2511.12832")
    assert verify_pdf_identity(pdf, expected).state == WRONG


def test_the_title_broken_across_lines_is_found():
    pdf = _pdf([_front("Attention Is All", "You Need", "Ashish Vaswani et al.")])
    verdict = verify_pdf_identity(pdf, ExpectedIdentity(title=PAPER))
    assert verdict.state == VERIFIED and verdict.signals == ["title-on-page"]


def test_the_embedded_title_rescues_a_page_whose_text_lost_the_title():
    pdf = _pdf([_front("ATTENTI0N 1S ALL Y0U NEED")], title=PAPER)  # OCR-style digits for letters
    verdict = verify_pdf_identity(pdf, ExpectedIdentity(title=PAPER))
    assert verdict.state == VERIFIED and verdict.signals == ["title-in-metadata"]


def test_a_scan_with_no_text_is_accepted_when_its_length_matches_the_record():
    pdf = _pdf(["" for _ in range(16)])
    verdict = verify_pdf_identity(pdf, ExpectedIdentity(title=PAPER, page_range="107-122"))
    assert verdict.state == VERIFIED and verdict.signals == ["page-count-corroborated"]


# --- what is refused ---------------------------------------------------------------------

def test_a_legible_document_naming_neither_doi_nor_title_is_wrong():
    """The motivating case: a landing-page scrape that followed a link to a cited report."""
    pdf = _pdf([_front("Clinical Practice Guideline of Major Depressive Disorder")])
    expected = ExpectedIdentity(doi="10.1371/journal.pone.0283095", title="Classification of Thai depression transcripts")
    verdict = verify_pdf_identity(pdf, expected)
    assert verdict.state == WRONG


def test_an_embedded_title_about_something_else_is_wrong():
    pdf = _pdf([_front("Body text with no title on it, only prose.")], title="Dietary Guidelines for Americans 2020-2025 Ninth Edition")
    verdict = verify_pdf_identity(pdf, ExpectedIdentity(title="Allergen immunotherapy in paediatric asthma: a review"))
    assert verdict.state == WRONG and "calls itself" in verdict.reason


def test_a_first_page_preview_is_truncated_even_though_it_carries_the_title():
    pdf = _pdf([_front(PAPER, "doi:10.1000/xyz.123")])
    verdict = verify_pdf_identity(pdf, ExpectedIdentity(doi="10.1000/xyz.123", title=PAPER, page_range="1-12"))
    assert verdict.state == TRUNCATED


def test_a_manuscript_longer_than_its_page_range_is_not_truncated():
    pdf = _pdf([_front(PAPER)] + [FILLER] * 20)
    verdict = verify_pdf_identity(pdf, ExpectedIdentity(title=PAPER, page_range="1-8"))
    assert verdict.state == VERIFIED


# --- what is not judged ------------------------------------------------------------------

def test_a_short_embedded_title_cannot_contradict_a_long_one():
    """'Allergy - Wiley Online Library' as /Title used to convict correct files."""
    pdf = _pdf([_front("prose only, the title never made it into the text layer")], title="Allergy")
    verdict = verify_pdf_identity(pdf, ExpectedIdentity(title="A very long article title about allergen immunotherapy in children"))
    assert verdict.state == WRONG  # refused for the *text*, not the metadata
    assert "calls itself" not in verdict.reason


def test_a_page_with_almost_no_text_is_unreadable_not_wrong():
    pdf = _pdf(["Downloaded from a library by guest on June 5, 2016"] * 3)
    verdict = verify_pdf_identity(pdf, ExpectedIdentity(title=PAPER))
    assert verdict.state == UNREADABLE
    assert not verdict.positive_rejection


def test_nothing_to_compare_against_is_no_reference():
    pdf = _pdf([_front("Whatever this is")])
    assert verify_pdf_identity(pdf, ExpectedIdentity()).state == NO_REFERENCE


# --- how the download stage uses it ------------------------------------------------------

def test_the_downloader_discards_a_wrong_file_and_leaves_nothing_behind(tmp_path):
    target = tmp_path / "paper.pdf"
    tmp = tmp_path / "paper.pdf.part"
    tmp.write_bytes(_pdf([_front("Clinical Practice Guideline of Major Depressive Disorder")]))
    downloader = PDFDownloader(DownloadConfig())
    with pytest.raises(WrongPaperError) as caught:
        downloader._check_identity(tmp, ExpectedIdentity(title=PAPER), final_url="https://example.org/x.pdf")
    assert caught.value.verdict["state"] == WRONG
    assert not tmp.exists() and not target.exists()


def test_the_downloader_keeps_a_file_it_could_not_check(tmp_path):
    tmp = tmp_path / "paper.pdf.part"
    tmp.write_bytes(_pdf(["" for _ in range(3)]))
    downloader = PDFDownloader(DownloadConfig())
    verdict = downloader._check_identity(tmp, ExpectedIdentity(title=PAPER), final_url="")
    assert verdict["state"] == UNREADABLE and tmp.exists()


def test_the_check_can_be_switched_off(tmp_path):
    tmp = tmp_path / "paper.pdf.part"
    tmp.write_bytes(_pdf([_front("Something else")]))
    downloader = PDFDownloader(DownloadConfig(verify_identity=False))
    assert downloader._check_identity(tmp, ExpectedIdentity(title=PAPER), final_url="") is None


def test_a_wrong_document_is_its_own_failure_reason():
    reason = DownloadOrchestrator._failure_reason(
        "download_failed_all_candidates",
        [{"wrong_document": True}, {"http_status": 404}],
        [],
    )
    assert reason == "wrong_document"
