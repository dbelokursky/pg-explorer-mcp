import unittest

import support
from pg_mcp.db import Database
from pg_mcp.tools import PROFILES, build_tools, profile_from_env


def tools_for(profile, warning=None):
    db = Database({"PGPORT": "not-a-number"})  # never connects: building tools needs no database
    return {tool.name: tool for tool in build_tools(db, support.ROOT, profile=profile, profile_warning=warning)}


class ProfileTests(unittest.TestCase):
    def test_profile_comes_from_the_environment(self):
        self.assertEqual(profile_from_env({}), ("explore", None))
        self.assertEqual(profile_from_env({"PG_MCP_PROFILE": " Analyst "}), ("analyst", None))

    def test_unknown_profile_falls_back_to_the_strictest_with_a_warning(self):
        profile, warning = profile_from_env({"PG_MCP_PROFILE": "admin"})
        self.assertEqual(profile, "schema")
        self.assertIn("PG_MCP_PROFILE='admin'", warning)
        status = tools_for(profile, warning)["status"].call({})
        self.assertIn("profile: schema (status, list_tables, describe_table)", status)
        self.assertIn("warning: PG_MCP_PROFILE='admin'", status)

    def test_each_profile_offers_its_tools_in_a_fixed_order(self):
        for profile, names in PROFILES.items():
            with self.subTest(profile=profile):
                self.assertEqual(tuple(tools_for(profile)), names)
        self.assertNotIn("run_select", tools_for("explore"))
        self.assertNotIn("sample_rows", tools_for("schema"))

    def test_only_analysts_may_ask_explain_to_run_the_query(self):
        self.assertNotIn("analyze", tools_for("explore")["explain_query"].properties)
        self.assertIn("analyze", tools_for("analyst")["explain_query"].properties)


if __name__ == "__main__":
    unittest.main()
