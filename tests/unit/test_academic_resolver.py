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
                    "student_name": "Landon Kelvin Jennings",
                    "course_school": "University of Colorado Colorado Springs",
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
                    "student_name": "Landon Kelvin Jennings",
                    "course_school": "University of Colorado Colorado Springs",
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
    assert out is not None
    response = str(out["response"])
    assert "Here are the classes found in your indexed academic records" not in response
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


def test_run_academic_resolver_ignores_suspicious_transcript_rows_from_agreements():
    collection = _MockCollection(
        {
            "documents": ["Course row: CSC 160 | Computer Science | Grade: I"],
            "metadatas": [
                {
                    "source": "/tmp/BC CSBA-Cyber 20-21 agreement.pdf",
                    "record_type": "transcript_row",
                    "course_code": "CSC 160",
                    "course_title": "Computer Science",
                }
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
    assert out is None


def test_run_academic_resolver_filters_transcript_rows_by_configured_identity():
    collection = _MockCollection(
        {
            "documents": [
                "Course row: CS 2060 | C Programming",
                "Course row: CS 4500 | Operating Systems I",
            ],
            "metadatas": [
                {
                    "source": "/tmp/Uccs_Transcript.pdf",
                    "record_type": "transcript_row",
                    "course_code": "CS 2060",
                    "course_title": "C Programming",
                    "student_name": "Landon Kelvin Jennings",
                    "course_school": "University of Colorado Colorado Springs",
                },
                {
                    "source": "/tmp/Uccs_Transcript.pdf",
                    "record_type": "transcript_row",
                    "course_code": "CS 4500",
                    "course_title": "Operating Systems I",
                    "student_name": "Other Person",
                    "course_school": "University of Colorado Colorado Springs",
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
        user_name="Landon Kelvin Jennings",
    )
    assert out is not None
    response = str(out["response"])
    assert "CS 2060" in response
    assert "CS 4500" not in response
    assert out["academic_identity_rows"] == 1
    assert out["academic_rows_pre_filter"] == 2


def test_run_academic_resolver_returns_constraint_no_match_response():
    collection = _MockCollection(
        {
            "documents": ["Course row: CS 2060 | C Programming"],
            "metadatas": [
                {
                    "source": "/tmp/Uccs_Transcript.pdf",
                    "record_type": "transcript_row",
                    "course_code": "CS 2060",
                    "course_title": "C Programming",
                    "student_name": "Other Person",
                    "course_school": "University of Colorado Colorado Springs",
                }
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
        user_name="Landon Kelvin Jennings",
    )
    assert out is not None
    assert out["guardrail_no_match"] is True
    assert out["guardrail_reason"] == "academic_identity_school_mismatch"
    assert "none matched the requested constraints" in str(out["response"]).lower()


def test_run_academic_resolver_applies_school_filter_with_aliases():
    collection = _MockCollection(
        {
            "documents": [
                "Course row: CS 2060 | C Programming",
                "Course row: MAT 201 | Calculus I",
            ],
            "metadatas": [
                {
                    "source": "/tmp/Uccs_Transcript.pdf",
                    "record_type": "transcript_row",
                    "course_code": "CS 2060",
                    "course_title": "C Programming",
                    "student_name": "Landon Kelvin Jennings",
                    "course_school": "CU Colo Springs",
                },
                {
                    "source": "/tmp/Community_College_Transcript.pdf",
                    "record_type": "transcript_row",
                    "course_code": "MAT 201",
                    "course_title": "Calculus I",
                    "student_name": "Landon Kelvin Jennings",
                    "course_school": "Pikes Peak State College",
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
        user_name="Landon Kelvin Jennings",
    )
    assert out is not None
    response = str(out["response"])
    assert "CS 2060" in response
    assert "MAT 201" not in response
    assert out["academic_school_rows"] == 1


def test_run_academic_resolver_keeps_attempted_not_completed_rows():
    collection = _MockCollection(
        {
            "documents": ["Course row: CS 4500 | Operating Systems I | Grade: W"],
            "metadatas": [
                {
                    "source": "/tmp/Uccs_Transcript.pdf",
                    "record_type": "transcript_row",
                    "course_code": "CS 4500",
                    "course_title": "Operating Systems I",
                    "course_grade": "W",
                    "course_status": "attempted_not_completed",
                    "student_name": "Landon Kelvin Jennings",
                    "course_school": "University of Colorado Colorado Springs",
                }
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
        user_name="Landon Kelvin Jennings",
    )
    assert out is not None
    response = str(out["response"])
    assert "CS 4500" in response
    assert "Status: attempted_not_completed" in response
