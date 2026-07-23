from types import SimpleNamespace

from app.reconciler.domain import LINK_ERROR, NO_BLOGGER, S_TEXT
from tools.evaluate import (_comparison_keys, _index_occurrences,
                            _known_noise_reason)


def test_evaluator_preserves_duplicate_business_keys():
    rows = [SimpleNamespace(campaign="W1", no="1", value="a"),
            SimpleNamespace(campaign="W1", no="1", value="b")]
    indexed = _index_occurrences(rows, lambda row: row.value)
    assert indexed == {("W1", "1", 1): "a", ("W1", "1", 2): "b"}


def test_evaluator_compares_missing_and_unexpected_rows():
    reference = {("W1", "1", 1): "MATCH", ("W1", "2", 1): "MATCH"}
    ours = {("W1", "1", 1): object(), ("W1", "3", 1): object()}
    assert _comparison_keys(reference, ours) == [
        ("W1", "1", 1), ("W1", "2", 1), ("W1", "3", 1)]


def test_tier_one_does_not_self_excuse_a_disagreement():
    verdict = SimpleNamespace(name="ordinary", post_date="2026-06-01",
                              tier="1:note-id-join")
    assert _known_noise_reason(verdict, S_TEXT[NO_BLOGGER], "MATCH") == ""


def test_known_noise_row_only_excuses_the_reviewed_transition():
    verdict = SimpleNamespace(name="兔子糖糖公主Rinrin",
                              post_date="2026-06-26",
                              matched_post_id="6a3e4f7a1234567890abcdef")
    assert _known_noise_reason(
        verdict, S_TEXT[NO_BLOGGER], "MATCH")
    assert _known_noise_reason(
        verdict, S_TEXT[NO_BLOGGER], S_TEXT[LINK_ERROR]) == ""

    different_note = SimpleNamespace(name="兔子糖糖公主Rinrin",
                                     post_date="2026-06-26",
                                     matched_post_id="ffffffff1234567890abcdef")
    assert _known_noise_reason(
        different_note, S_TEXT[NO_BLOGGER], "MATCH") == ""
