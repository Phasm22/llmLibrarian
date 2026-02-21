from tax.normalize import extract_tax_year, tokenize_entity


def test_tokenize_entity_splits_alpha_digit_tokens():
    tokens = tokenize_entity("Federal_Colorado_deloitte2025")
    assert "deloitte" in tokens
    assert "2025" not in tokens


def test_extract_tax_year_prefers_filename_over_parent_folder():
    source = "/Users/x/Documents/Tax/2022/2021_TaxReturn.pdf"
    assert extract_tax_year(source, "") == 2021


def test_extract_tax_year_uses_first_filename_year_when_multiple_present():
    source = "/Users/x/Documents/Tax/2022/W-2_Form_2021_JENKINS_2022_01_13.pdf"
    assert extract_tax_year(source, "") == 2021
