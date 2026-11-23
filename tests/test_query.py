from app.query import parse_query


def test_reads_cities_remote_and_india_out_of_the_query():
    p = parse_query("remote backend python jobs in Pune")
    assert (p.text, p.cities, p.remote, p.india) == ("backend python", ["Pune"], True, False)
    p = parse_query("ML engineer, Bangalore or gurgaon")
    assert p.text == "ML engineer" and p.cities == ["Bengaluru", "Gurugram"]
    p = parse_query("work from home data analyst roles across India")
    assert (p.text, p.remote, p.india) == ("data analyst", True, True)
    assert parse_query("android intern based in ncr").cities == ["Delhi NCR"]


def test_only_filters_leaves_no_text():
    p = parse_query("jobs in hyderabad")
    assert p.text == "" and p.cities == ["Hyderabad"] and p.has_filters


def test_queries_without_filters_are_untouched():
    for q in ["backend engineer jobs", "golang developer", "Indiana Jones fan"]:
        p = parse_query(q)
        assert p.text == q and not p.has_filters
