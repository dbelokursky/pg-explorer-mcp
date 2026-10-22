import unittest

import support  # noqa: F401  (sets up sys.path)
from pg_mcp.budget import DEFAULT_BUDGET, MIN_BUDGET, budget_from_env, fit_to_budget, shorten

HEADER = "sales.orders | table | ~1.2k rows | Заказы клиентов"


def answer(columns: int, width: int = 60) -> list:
    return [HEADER, "columns:"] + [f"  column_{i:03d} text  -- " + "x" * width for i in range(columns)]


class FitToBudgetTests(unittest.TestCase):
    """The contract in fit_to_budget's docstring."""

    def fit(self, lines, budget):
        result = fit_to_budget(list(lines), budget)
        self.assertLessEqual(len("\n".join(result)), budget)
        if result != lines:
            self.assertTrue(result and result[-1].startswith("…"), f"no notice at the end: {result[-1:]}")
            kept = result[:-1]
            if kept:
                self.assertEqual(kept[:-1], lines[: len(kept) - 1], "kept lines must be the input's first lines")
                last = kept[-1].rstrip("…")
                self.assertTrue(lines[len(kept) - 1].startswith(last), "the last kept line may only be shortened")
        return result

    def test_answer_that_fits_comes_back_unchanged(self):
        lines = answer(5)
        self.assertEqual(self.fit(lines, DEFAULT_BUDGET), lines)

    def test_answer_of_exactly_the_budget_is_not_cut(self):
        lines = answer(3)
        self.assertEqual(self.fit(lines, len("\n".join(lines))), lines)

    def test_long_answer_is_cut_with_a_notice_and_keeps_the_header(self):
        lines = answer(500)
        for budget in (MIN_BUDGET, 1000, DEFAULT_BUDGET):
            with self.subTest(budget=budget):
                result = self.fit(lines, budget)
                self.assertNotEqual(result, lines)
                self.assertEqual(result[0], HEADER)

    def test_one_huge_line_still_fits(self):
        self.fit(["x" * 20000], MIN_BUDGET)

    def test_empty_answer_stays_empty(self):
        self.assertEqual(fit_to_budget([], MIN_BUDGET), [])


class ShortenTests(unittest.TestCase):
    def test_collapses_whitespace_into_one_line(self):
        self.assertEqual(shorten("Заказы\n  клиентов\tза год"), "Заказы клиентов за год")

    def test_cuts_long_values_with_an_ellipsis(self):
        value = shorten("a" * 500, limit=20)
        self.assertEqual(len(value), 20)
        self.assertTrue(value.endswith("…"))


class BudgetFromEnvTests(unittest.TestCase):
    def test_default_number_and_floor(self):
        self.assertEqual(budget_from_env({}), DEFAULT_BUDGET)
        self.assertEqual(budget_from_env({"PG_MCP_MAX_RESPONSE_CHARS": "12000"}), 12000)
        self.assertEqual(budget_from_env({"PG_MCP_MAX_RESPONSE_CHARS": "10"}), MIN_BUDGET)

    def test_garbage_falls_back_to_the_default(self):
        with self.assertLogs("pg_mcp.budget", "WARNING"):
            self.assertEqual(budget_from_env({"PG_MCP_MAX_RESPONSE_CHARS": "lots"}), DEFAULT_BUDGET)


if __name__ == "__main__":
    unittest.main()
