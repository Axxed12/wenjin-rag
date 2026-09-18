from scripts.recover_table_demo import recover_first_five


def test_recover_first_five_business_relations():
    records = recover_first_five()
    assert [record["state"] for record in records] == [
        "Alabama", "Alaska", "Arizona", "Arkansas", "California"
    ]
    assert [record["permit"] for record in records] == [
        "18,870", "209", "2,303", "3,298", "98 452"
    ]
    assert [record["totals"] for record in records] == [
        "71,137", "7,095", "27,087", "25,048", "180 116"
    ]
