from app.reconciler.name_match import name_contains, name_ladder
from app.core.textnorm import ascii_part, cjk, header_key, norm
from app.reconciler.domain import is_hex24


def test_norm_strips_emoji():
    assert norm("一颗鸡蛋🥚") == "一颗鸡蛋"


def test_norm_strips_zwj_and_variation_selectors():
    assert norm("a‍b️") == "ab"


def test_norm_collapses_whitespace_and_casefolds():
    assert norm("  Poppy - Chan ") == "poppy-chan"


def test_header_key_fullwidth_paren():
    assert header_key("FAN BASE（K)") == "fanbase(k)"


def test_header_key_double_space():
    assert header_key("TTL  ENGAGEMENT") == "ttlengagement"


def test_cjk_extraction():
    assert cjk("莉莉安Lilian") == "莉莉安"
    assert cjk("gungun_") == ""
    assert cjk("曹熙珺Cee") == "曹熙珺"


def test_ascii_part_strips_trailing_punct():
    assert ascii_part("gungun_") == "gungun"
    assert ascii_part("shen02") == "shen02"
    assert ascii_part("Poppy-chan") == "poppy-chan"
    assert ascii_part("莉莉安Lilian") == "lilian"


def test_is_hex24():
    assert is_hex24("6a3e4f7a0000000010001234")
    assert not is_hex24("6a3e4f7a00000000100012")   # 22 chars
    assert not is_hex24("6a3e4f7a000000001000123z")


# ---------------------------------------------------------------- ladder

def test_ladder_cjk_substring():
    assert name_ladder("Aimee三岁", "Aimee San Sui Aimee三岁") == "cjk-substring"
    assert name_ladder("墨池墨吟", "墨池墨吟") == "cjk-substring"


def test_ladder_norm_substring():
    assert name_ladder("Lilian", "Li Li An Lilian") == "norm-substring"


def test_ladder_ascii_fuzzy_gungun():
    assert name_ladder("gungun_", "gungunnnnn") == "ascii-fuzzy"


def test_ladder_requires_min_handle_len():
    # 3-char latin handles must not fuzzy-match junk
    assert name_ladder("ab_", "abbbbbb") == ""


def test_ladder_pinyin_bridge():
    assert name_ladder("饼饼", "Bing Bing") == "pinyin-bridge"


def test_ladder_no_match():
    assert name_ladder("子回頭是浪", "Chilly Pan Pan Chilly潘潘") == ""


# ------------------------------------------------- strict containment nuance

def test_name_contains_strict_rejects_fuzzy():
    # the human flags gungun_ recorded as gungunnnnn — strict containment fails
    assert not name_contains("gungun_", "gungunnnnn")


def test_name_contains_accepts_decorated():
    assert name_contains("Aimee三岁", "Aimee San Sui Aimee三岁")
    assert name_contains("一颗鸡蛋🥚", "Yi Ke Ji Dan 一颗鸡蛋")
