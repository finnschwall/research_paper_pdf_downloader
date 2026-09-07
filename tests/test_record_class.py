"""What the record classifier must and must not claim.

The negative cases matter more than the positive ones here. A false positive removes a real
paper from a systematic review and reports it as "nothing to fetch", which nobody rechecks.
"""
from paper_downloader.metadata import record_class as rc


def test_openalex_type_alone_settles_a_conference_abstract():
    verdict = rc.classify(openalex={"type": "conference-abstract"})
    assert verdict.record_class == rc.CONFERENCE_ABSTRACT
    assert verdict.terminal


def test_aacr_abstract_from_supplement_and_title():
    verdict = rc.classify(
        crossref={
            "issue": "16_Supplement",
            "container-title": ["Cancer Research"],
            "title": ["Abstract 4137: A study of something"],
        },
    )
    assert verdict.record_class == rc.CONFERENCE_ABSTRACT


def test_ats_session_code_in_a_supplement():
    verdict = rc.classify(
        crossref={
            "issue": "Supplement 1",
            "container-title": ["American Journal of Respiratory and Critical Care Medicine"],
            "title": ["A1234-5678 Some Respiratory Finding"],
        },
    )
    assert verdict.record_class == rc.CONFERENCE_ABSTRACT


def test_a_title_pattern_alone_is_not_enough():
    """A numbering scheme with no supplement and no abstract book is a coincidence."""
    verdict = rc.classify(
        crossref={"container-title": ["Journal of Materials Science"],
                  "title": ["316P a Novel Alloy Under Load"]},
    )
    assert verdict.record_class is None


def test_a_paper_about_abstraction_is_not_an_abstract():
    verdict = rc.classify(crossref={"title": ["Abstract Counterfactuals for Neural Networks"]})
    assert verdict.record_class is None


def test_morressier_prefix_is_a_poster():
    verdict = rc.classify(crossref={"DOI": "10.26226/m.abcdef", "type": "posted-content"})
    assert verdict.record_class == rc.POSTER


def test_posted_content_of_no_subtype_is_a_poster():
    verdict = rc.classify(crossref={"type": "posted-content", "subtype": "other"})
    assert verdict.record_class == rc.POSTER


def test_a_preprint_is_not_a_poster():
    verdict = rc.classify(crossref={"type": "posted-content", "subtype": "preprint"})
    assert verdict.record_class is None


def test_corrections_by_title():
    for title in ("Erratum: A paper", "Corrigendum to a paper", "Correction to: A paper",
                  "Publisher Correction: A paper"):
        assert rc.classify(crossref={"title": [title]}).record_class == rc.CORRECTION


def test_dataset_is_not_an_article():
    assert rc.classify(crossref={"type": "dataset"}).record_class == rc.NOT_AN_ARTICLE


def test_paratext_from_openalex_flag():
    assert rc.classify(openalex={"is_paratext": True}).record_class == rc.PARATEXT


def test_retraction_is_a_flag_and_never_terminal():
    verdict = rc.classify(
        crossref={"type": "journal-article", "update-to": [{"type": "retraction"}]},
    )
    assert verdict.retracted is True
    assert verdict.record_class is None
    assert not verdict.terminal


def test_openalex_retraction_flag():
    assert rc.classify(openalex={"is_retracted": True}).retracted is True


def test_published_online_is_read_off_crossref():
    verdict = rc.classify(crossref={"published-online": {"date-parts": [[2026, 8, 3]]}})
    assert verdict.published_online == "2026-08-03"


def test_a_plain_article_gets_no_class_at_all():
    verdict = rc.classify(
        crossref={"type": "journal-article", "title": ["Deep Learning for Something"]},
        openalex={"type": "article"},
    )
    assert verdict.record_class is None
    assert verdict.retracted is False


def test_every_terminal_class_is_a_known_class():
    assert rc.TERMINAL_CLASSES <= set(rc.RECORD_CLASSES)
    assert rc.RETRACTED not in rc.TERMINAL_CLASSES
