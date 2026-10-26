# Profile charts

Refresh public and private language data using your existing authenticated GitHub CLI login:

```sh
python scripts/update_profile_charts.py --include-private
```

The signed-in account must match the profile owner. The script uses `gh api` to
paginate owned repositories and retrieve their language byte counts. It excludes
forks and aggregates all remaining repositories, including older project versions.

Only totals are written to `assets/stats/profile-data.json`: language bytes,
repository count, scope and dates. Repository names, visibility flags and
per-repository language data remain in memory and are not published.
The language chart groups languages after the top five into Other. It describes
code in these repositories, not personal proficiency.

Render the existing snapshot without network requests:

```sh
python scripts/update_profile_charts.py --from-cache
```

Refresh the public activity calendar while preserving the private-inclusive
language snapshot and its original update date:

```sh
python scripts/update_profile_charts.py --preserve-languages
```

A plain invocation refuses to replace a private-inclusive snapshot with a
public-only scan. The script fetches and renders all assets before replacing the
previous successful files. Unknown or missing contribution data causes a failure
rather than substituting zeroes.

The activity chart uses the public GitHub profile calendar. It shows twelve
calendar-week columns, including the current week through today. Its counts follow
GitHub's contribution definition and are not commit counts.

The scheduled workflow refreshes activity daily at 03:17 UTC and preserves the
language snapshot. The built-in workflow token cannot read other private
repositories. Refresh private-inclusive language totals locally using the first
command above; unattended refresh would need separately configured cross-repository
read access. No additional credential has been configured.

Scheduled runs start after publishing the workflow on the default branch.
Repository policies must allow its chart commits. GitHub may delay scheduled
runs and disables schedules on inactive public repositories after 60 days.

Checks: `python -B scripts/test_profile_charts.py`.

Sources:
- [GitHub CLI API pagination](https://cli.github.com/manual/gh_api)
- [GitHub language data](https://docs.github.com/en/rest/repos/repos#list-repository-languages)
- [Public contribution calendar](https://github.com/users/PatrykBr/contributions)
- [Workflow token permissions](https://docs.github.com/en/actions/tutorials/authenticate-with-github_token)
