"""BibTeX, DOI-list and work-list inputs, and title normalisation."""

from conftest import FIXTURES

from oacurate.bib import norm_title, read_bib, read_doi_list, records_from_works

SCHOLAR = FIXTURES / "bib" / "scholar.synthetic.bib"


def test_scholar_export_is_parsed_and_every_failure_is_reported():
    records, unresolved = read_bib(SCHOLAR)
    assert [r.key for r in records] == ["muller2024koopman", "doe2019wavelets"]
    reasons = {(u.reason, u.ref) for u in unresolved}
    assert ("duplicate-key", "bib:muller2024koopman") in reasons
    parse_errors = [u for u in unresolved if u.reason == "parse-error"]
    assert len(parse_errors) == 1
    assert ":9:" in parse_errors[0].detail and "broken2020" in parse_errors[0].detail


def test_latex_is_decoded_and_others_is_flagged():
    first = read_bib(SCHOLAR)[0][0]
    assert first.authors == ["Müller, Jörg", "O'Brien, Pat"]
    assert first.authors_truncated
    assert first.title == "Koopman Operators " + chr(0x2013) + " a Survey"
    assert first.arxiv_id == "2401.01234"
    assert first.doi is None
    assert first.venue == "arXiv preprint arXiv:2401.01234"


def test_identifiers_come_from_the_undecoded_fields():
    # LaTeX decoding would turn "--" into an en dash inside the URL and corrupt the DOI.
    second = read_bib(SCHOLAR)[0][1]
    assert second.doi == "10.1000/a--b_c"


def test_norm_title_handles_en_dashes_and_braces():
    dash = chr(0x2013)
    assert norm_title(f"Koopman {{O}}perators {dash} a {{S}}urvey") == "koopman operators a survey"
    assert norm_title("Koopman Operators - A Survey") == "koopman operators a survey"
    assert norm_title("Müller" + chr(0x2019) + "s $x$") == "muller s x"


def test_doi_list_skips_comments_and_reports_bad_lines(tmp_path):
    path = tmp_path / "dois.txt"
    path.write_text("# header\n10.7717/peerj.4375\n\nhttps://doi.org/10.1000/abcd  # note\nnope\n")
    records, unresolved = read_doi_list(path)
    assert [r.doi for r in records] == ["10.7717/peerj.4375", "10.1000/abcd"]
    assert len(unresolved) == 1 and unresolved[0].reason == "invalid-identifier"
    assert f"{path}:5" in unresolved[0].detail


def test_work_list_normalises_and_rejects():
    records, unresolved = records_from_works(["w123", "https://openalex.org/W456", "A1"])
    assert [r.work_id for r in records] == [
        "https://openalex.org/W123",
        "https://openalex.org/W456",
    ]
    assert [u.ref for u in unresolved] == ["work:A1"]


def test_a_hash_inside_a_doi_is_not_a_comment(tmp_path):
    path = tmp_path / "dois.txt"
    path.write_text("10.1000/a#b\n10.1000/c # trailing note\n# whole-line comment\n")
    records, unresolved = read_doi_list(path)
    assert [r.doi for r in records] == ["10.1000/a#b", "10.1000/c"] and unresolved == []
