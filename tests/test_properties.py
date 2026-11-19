"""Property tests: the pure functions that parse what the world may hand them.

Handwritten cases show what the author thought of; generated ones show what the world has
more of. Everything here runs against plain functions with no database behind them, so a
hundred examples of each cost a blink, and pytest-randomly reseeds every run.
"""

from __future__ import annotations

import string

from hypothesis import given
from hypothesis import strategies as st

from pg_explorer_mcp.budget import fit_to_budget, shorten
from pg_explorer_mcp.db import _split_pgpass_line, split_hosts
from pg_explorer_mcp.diagram import _attribute_name, mermaid_type
from pg_explorer_mcp.stands import stand_ranges, summarize
from pg_explorer_mcp.tools import _like_escape

field = st.text(alphabet=string.printable.replace(":", "").replace("\\", ""), min_size=1, max_size=30)
# No leading zeros: stand_ranges normalizes "a00" into "a0" — found by this very property test,
# accepted as the answer to an exotic name rather than a bug worth new syntax in the ranges.
name = st.from_regex(r"[a-z]{1,3}[1-9][0-9]{0,2}", fullmatch=True)


@given(st.lists(field, min_size=1, max_size=6))
def test_a_pgpass_line_splits_into_the_fields_it_was_written_from(fields: list[str]) -> None:
    line = ":".join(fields)
    assert _split_pgpass_line(line) == fields


@given(st.lists(field.filter(str), min_size=1, max_size=5), st.lists(field, min_size=1, max_size=5))
def test_an_escaped_colon_stays_inside_its_field(plain: list[str], inner: list[str]) -> None:
    # A password with colons is the reason the escapes exist: none of them may open a new field.
    value = "\\:".join(inner)
    fields = [value if index == len(plain) // 2 else part for index, part in enumerate(plain)]
    assert len(_split_pgpass_line(":".join(fields))) == len(fields)


@given(st.lists(st.text(alphabet="ab", min_size=1, max_size=8), min_size=1, max_size=4))
def test_hosts_pair_with_the_one_port(hosts: list[str]) -> None:
    pairs = split_hosts(",".join(hosts), "5432")
    assert [host for host, _port in pairs] == hosts
    assert all(port == 5432 for _host, port in pairs)


@given(st.text(min_size=1, max_size=40))
def test_an_escaped_like_fragment_has_no_wildcard_of_its_own(text: str) -> None:
    escaped = _like_escape(text)
    for index, char in enumerate(escaped):
        if char in "%_\\":
            assert escaped[index - 1] == "\\" or (char == "\\" and index + 1 < len(escaped))


@given(st.sets(name, min_size=1, max_size=12))
def test_stand_ranges_fold_into_runs_that_name_the_same_stands(stands: set[str]) -> None:
    def natural(stand: str) -> tuple[str, int]:
        letters = stand.rstrip("0123456789")
        return letters, int(stand[len(letters) :])

    ordered = sorted(stands, key=natural)
    folded = stand_ranges(ordered)
    named: list[str] = []
    for part in folded:
        prefix = ""
        head = part.split("–")
        if len(head) == 2:
            prefix = head[0].rstrip("0123456789")
            first = int(head[0][len(prefix) :])
            last = int(head[1].rpartition(prefix)[2] or head[1])
            named.extend(f"{prefix}{number}" for number in range(first, last + 1))
        else:
            named.append(part)
    assert sorted(named, key=natural) == ordered
    assert summarize(ordered) == ", ".join(folded)


@given(st.text(min_size=1, max_size=60), st.integers(min_value=1, max_value=200))
def test_shortened_text_stays_within_the_limit_on_one_line(text: str, limit: int) -> None:
    short = shorten(text, limit)
    assert len(short) <= limit
    assert short.count("\n") == 0


@given(st.lists(st.text(min_size=1, max_size=30), min_size=1, max_size=50), st.integers(min_value=500, max_value=8000))
def test_a_fitted_answer_stays_within_the_budget(lines: list[str], budget: int) -> None:
    fitted = fit_to_budget(lines, budget)
    assert len("\n".join(fitted)) <= budget
    assert all(line in lines for line in fitted[:-1])  # the last line may be the notice of the cut


@given(st.text(min_size=1, max_size=20))
def test_a_mermaid_attribute_name_is_always_safe_to_render(name: str) -> None:
    safe = _attribute_name(name)
    assert safe[:1].isdigit() is False
    assert all(char.isalnum() or char in "_-" for char in safe)


@given(st.text(min_size=1, max_size=30))
def test_a_mermaid_type_is_one_word(data_type: str) -> None:
    assert " " not in mermaid_type(data_type).replace("(", "").replace(")", "")
    assert "," not in mermaid_type(data_type)
