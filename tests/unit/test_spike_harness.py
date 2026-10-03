from spikes.harness.corpus import Chunk, classify_shape, fixture_digest, histogram
from spikes.harness.power import parse_powermetrics


def test_shape_classification_is_specific_before_pdf():
    assert classify_shape({"doc_type": "tax_return", "source": "/x/form.pdf"}, "") == "tax-financial"
    assert classify_shape({"doc_type": "pdf", "source": "/x/report.pdf"}, "") == "pdf-layout"
    assert classify_shape({"source": "/x/main.py"}, "") == "source-code"
    assert classify_shape({"source": "/x/call.txt"}, "Speaker 1: hello") == "transcripts"
    assert classify_shape({"source": "/x/note.md"}, "ordinary prose") == "prose-notes"


def test_histogram_preserves_count_at_ceiling():
    result = histogram([1, 32, 33, 383, 384, 900])
    assert sum(result.values()) == 6
    assert result["384+"] == 2


def test_fixture_digest_depends_on_text_and_order():
    a = Chunk("1", "alpha", {}, 3, "prose-notes")
    b = Chunk("2", "beta", {}, 3, "prose-notes")
    assert fixture_digest([a, b]) != fixture_digest([b, a])
    assert fixture_digest([a]) != fixture_digest([Chunk("1", "changed", {}, 3, "prose-notes")])


def test_powermetrics_parser_uses_primary_rail_block():
    raw = """
*** Sampled system activity (date) (250ms elapsed) ***
CPU Power: 1000 mW
GPU Power: 200 mW
ANE Power: 3 mW
Combined Power (CPU + GPU + ANE): 1203 mW
**** GPU usage ****
GPU HW active frequency: 618 MHz
GPU HW active residency:  25.00% (338 MHz: 1%)
GPU idle residency:  75.00%
GPU Power: 190 mW
"""
    parsed = parse_powermetrics(raw)
    assert parsed["mean"]["combined_mw"] == 1203
    assert parsed["mean"]["gpu_mw"] == 200
    assert parsed["mean"]["gpu_frequency_mhz"] == 618
    assert parsed["mean"]["gpu_idle_pct"] == 75
