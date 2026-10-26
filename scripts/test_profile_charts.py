"""Checks for private aggregation, account scope, and scheduled snapshot preservation."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import update_profile_charts as charts


class ProfileChartTests(unittest.TestCase):
    def test_private_repos_are_included_and_forks_and_other_owners_excluded(self):
        def repo(name, private=False, fork=False, owner="PatrykBr"):
            return {"full_name": owner + "/" + name, "owner": {"login": owner},
                    "private": private, "fork": fork}

        pages = [[repo("public-project"), repo("private-project", private=True)],
                 [repo("fork", private=True, fork=True), repo("foreign", owner="someone-else")]]
        calls = []

        def gh(endpoint, paginate=False):
            calls.append((endpoint, paginate))
            if endpoint == "user":
                return {"login": "PatrykBr"}
            if endpoint.startswith("user/repos?"):
                self.assertTrue(paginate)
                return pages
            if endpoint == "repos/PatrykBr/public-project/languages":
                return {"TypeScript": 50, "Python": 10}
            if endpoint == "repos/PatrykBr/private-project/languages":
                return {"Go": 30, "Python": 10}
            self.fail("Unexpected repository access")

        with patch.object(charts, "gh_json", side_effect=gh):
            data = charts.language_data("PatrykBr", include_private=True)
        self.assertEqual(data["repository_count"], 2)
        self.assertEqual(data["languages"], {"TypeScript": 50, "Go": 30, "Python": 20})
        self.assertEqual(data["language_scope"], "public_and_private")
        serialized = json.dumps(data)
        for identifier in ("public-project", "private-project", "PatrykBr/"):
            self.assertNotIn(identifier, serialized)
        self.assertIn(("user/repos?affiliation=owner&per_page=100", True), calls)

    def test_wrong_signed_in_account_is_rejected(self):
        with patch.object(charts, "gh_json", return_value={"login": "another-account"}) as gh:
            with self.assertRaises(ValueError):
                charts.language_data("PatrykBr", include_private=True)
        self.assertEqual(gh.call_count, 1)

    def test_cli_errors_do_not_disclose_private_names(self):
        result = subprocess.CompletedProcess([], 1, "", "private-project-name: permission denied")
        with patch.object(charts.subprocess, "run", return_value=result):
            with self.assertRaises(RuntimeError) as caught:
                charts.gh_json("repos/PatrykBr/private-project-name/languages")
        self.assertNotIn("private-project-name", str(caught.exception))

    def test_scheduled_refresh_preserves_private_language_data_and_date(self):
        today = datetime.now(timezone.utc).date()
        start = today - timedelta(days=(today.weekday() + 1) % 7 + 77)
        html = ""
        for offset in range((today - start).days + 1):
            day = (start + timedelta(days=offset)).isoformat()
            html += (f'<td id="d{offset}" data-date="{day}" data-level="0"></td>'
                     f'<tool-tip for="d{offset}">No contributions on this day.</tool-tip>')
        previous = {"login": "PatrykBr", "repository_count": 41, "language_scope": "public_and_private",
                    "languages_updated": "2026-01-01", "languages": {"TypeScript": 70, "Go": 30}}
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            (output / "profile-data.json").write_text(json.dumps(previous), encoding="utf-8")
            with patch.object(charts, "OUTPUT", output), patch.object(charts, "fetch", return_value=html):
                with patch.object(charts, "language_data", side_effect=AssertionError("Should preserve snapshot")):
                    data = charts.collect("PatrykBr", preserve_languages=True)
        for key in ("repository_count", "language_scope", "languages_updated", "languages"):
            self.assertEqual(data[key], previous[key])
        self.assertEqual(data["updated"], today.isoformat())


if __name__ == "__main__":
    unittest.main()
