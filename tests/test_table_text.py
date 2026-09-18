from wenjin.ingestion.table_text import record_to_text, records_to_chunks


def test_table_text_keeps_row_and_column_context():
    record = {
        "state": "Alabama",
        "permit": "18,870",
        "totals": "71,137",
        "source": {"file": "original.pdf", "page_number": 1, "bbox": [1, 2, 3, 4]},
    }
    text = record_to_text(record, title="Checks", period="2015")
    assert "Alabama" in text and "Permit：18,870" in text and "Checks" in text
    chunk = records_to_chunks([record], title="Checks", period="2015")[0]
    assert chunk["metadata"]["row_key"] == "Alabama"
    assert chunk["metadata"]["page_number"] == 1
