from cbioportal_mcp import server


def test_all_numbered_pitfalls_are_parsed():
    sections = server._common_pitfall_sections()

    for number in ["1", "2", "3", "4", "5", "5b", "5c", "6", "7", "8", "9", "10",
                    "11", "12", "13", "14", "15", "16", "17", "18", "19", "20", "21"]:
        assert number in sections, f"pitfall #{number} missing from parsed sections"


def test_pitfall_numbers_are_unique():
    numbers = server._PITFALL_HEADER_RE.findall(server._common_pitfalls_guide_text())

    duplicates = sorted({n for n in numbers if numbers.count(n) > 1})
    assert not duplicates, f"pitfall numbers used twice (one section becomes unreachable): {duplicates}"


def test_lettered_pitfalls_are_each_reachable():
    assert "IMPLIED LITERATURE REVIEW" in server.read_guide.fn("cbioportal://common-pitfalls#17b")
    assert "AMBIGUOUS ACC" in server.read_guide.fn("cbioportal://common-pitfalls#17c")
    assert "LEFT- VS RIGHT-SIDED" in server.read_guide.fn("cbioportal://common-pitfalls#17d")


def test_fragment_returns_only_the_requested_pitfall():
    fragment = server.read_guide.fn("cbioportal://common-pitfalls#16")

    assert "SILENT QUERY SUBSTITUTION" in fragment
    assert "CRITICAL MUTATION FREQUENCY ERRORS" not in fragment
    assert "ENUMERATION / CATALOG QUESTIONS" not in fragment


def test_fragment_is_much_smaller_than_full_guide():
    full_guide = server.read_guide.fn("cbioportal://common-pitfalls")
    fragment = server.read_guide.fn("cbioportal://common-pitfalls#16")

    assert len(fragment.split()) < len(full_guide.split()) / 5


def test_unknown_pitfall_number_lists_available_numbers_instead_of_crashing():
    result = server.read_guide.fn("cbioportal://common-pitfalls#999")

    assert "No pitfall numbered '999'" in result
    assert "16" in result
    assert "cbioportal://common-pitfalls" in result


def test_full_guide_is_unchanged_and_still_readable():
    result = server.read_guide.fn("cbioportal://common-pitfalls")

    assert "FLAWED PREMISE OR NONEXISTENT DATA FIELD" in result
    assert "SILENT QUERY SUBSTITUTION" in result


def test_system_prompt_routes_pitfall_16_and_21_to_fragment_uris():
    prompt = server._load_resource("system-prompt.md")

    assert "cbioportal://common-pitfalls#16" in prompt
    assert "cbioportal://common-pitfalls#21" in prompt
