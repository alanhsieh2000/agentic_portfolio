"""One autouse safety net: no test may write into the repository's own
`output/`.

`src/flow/report_archive.py` archives every report the two entry points print,
and both `main()` functions build a real archive from `settings.output_dir`.
Around fifty tests in `tests/test_cli.py` invoke `main()` by monkeypatching
`sys.argv`, so without this fixture the suite would start filling
`/app/agentic_portfolio/output/` with hundreds of files.

Why a fixture rather than `--output-dir` on every argv builder. Every other
path in this suite IS redirected per test by a flag - `--memory-path`,
`--rates-path`, `--holdings-path`, `--db-path`, all pointed at `tmp_path` - and
that is still the right pattern for a test that is ABOUT the archive, which
passes `--output-dir` explicitly. But adding the flag to fifty existing argv
builders would only guarantee that the fifty-first, written later, gets
forgotten. The failure mode is invisible: `output/` is gitignored, so a
polluting test leaves `git status` clean and nothing ever complains. A guard
that cannot be forgotten is worth more here than consistency with a convention
whose whole weakness is that it must be remembered.

This is the only `conftest.py` in the repository and the only fixture in the
suite; `AGENTS.md` and the surrounding test modules otherwise use plain
`_`-prefixed helper functions taking `monkeypatch` and `tmp_path`. Deviating
once, for a repository-pollution guard, is deliberate - see
`plans/18_saved_report_archive.md`'s Decision Log.
"""

import pytest

from src.config.settings import settings


@pytest.fixture(autouse=True)
def _archive_reports_under_tmp_path(monkeypatch, tmp_path):
    """Point `settings.output_dir` at this test's own `tmp_path`.

    Set on the settings singleton rather than on each module that reads it,
    because both `src/flow/cli.py` and `src/flow/holdings_cli.py` read
    `settings.output_dir` at parse time as their `--output-dir` default, and
    the singleton is the one object they share. `monkeypatch` restores it after
    every test, so nothing leaks between them.
    """
    monkeypatch.setattr(settings, "output_dir", str(tmp_path / "archived-reports"))
