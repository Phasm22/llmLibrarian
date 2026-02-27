from query.academic_resolver import run_academic_resolver


class _MockCollection:
    def __init__(self, get_result):
        self.get_result = get_result
        self.calls = []

    def get(self, **kwargs):
        self.calls.append(kwargs)
        return self.get_result


def test_run_academic_resolver_returns_deduped_rows_with_confidence_labels():
    collection = _MockCollection(
        {
            "documents": [
                "Course row: CS 2060 | C Programming | Term: Fall 2023 | Credits: 4.000 | Grade: A",
                "Course row: CS 2060 | C Programming | Term: Fall 2023 | Credits: 4.000 | Grade: A",
                "Course row: CNG 1002 | Local Area Networks | Term: Fall 2022",
            ],
            "metadatas": [
                {
                    "source": "/tmp/Uccs_Transcript.pdf",
                    "record_type": "transcript_row",
                    "course_code": "CS 2060",
                    "course_title": "C Programming",
                    "course_term": "Fall 2023",
                    "course_grade": "A",
                    "course_credits": "4.000",
                    "page": 1,
                },
                {
                    "source": "/tmp/Uccs_Transcript.pdf",
                    "record_type": "transcript_row",
                    "course_code": "CS 2060",
                    "course_title": "C Programming",
                    "course_term": "Fall 2023",
                    "course_grade": "A",
                    "course_credits": "4.000",
                    "page": 1,
                },
                {
                    "source": "/tmp/degree-audit.pdf",
                    "record_type": "audit_row",
                    "course_code": "CNG 1002",
                    "course_title": "Local Area Networks",
                    "course_term": "Fall 2022",
                    "page": 3,
                },
            ],
        }
    )
    out = run_academic_resolver(
        query_contract={
            "mode": "classes_taken",
            "completed_only": True,
            "requested_school": "UCCS",
            "requested_term": None,
            "requested_year": None,
        },
        collection=collection,
        use_unified=True,
        silo="old-school",
        source_label="old-school",
        no_color=True,
    )
    assert out is not None
    response = str(out["response"])
    assert "[High]" in response
    assert "[Medium]" in response
    assert "1. [Medium] CNG 1002 Local Area Networks" in response
    assert "2. [High] CS 2060 C Programming" in response
    assert out["academic_transcript_hits"] == 1
    assert out["academic_evidence_rows"] == 2
    assert out["num_docs"] == 2


def test_run_academic_resolver_returns_none_when_no_course_rows():
    collection = _MockCollection(
        {
            "documents": ["General transfer agreement text"],
            "metadatas": [{"source": "/tmp/guide.pdf", "doc_type": "other"}],
        }
    )
    out = run_academic_resolver(
        query_contract={
            "mode": "classes_taken",
            "completed_only": True,
            "requested_school": None,
            "requested_term": None,
            "requested_year": None,
        },
        collection=collection,
        use_unified=True,
        silo="old-school",
        source_label="old-school",
        no_color=True,
    )
    assert out is None
