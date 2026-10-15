"""Is the deployed watcher still alive, and will it stay that way?

Two ways it dies silently:

1. GitHub disables scheduled workflows in a public repo after 60 days without
   repository activity, and reports suggest only commits to the default branch
   count. The watcher's own pushes go to fareloot-data, and main is protected,
   so nothing automated can keep it alive. A person has to merge something.
2. Scheduled runs get throttled, dropped, or keep failing, and nobody looks at
   the Actions tab.

These checks run daily and warn early enough for a person to act.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

INACTIVITY_LIMIT_DAYS = 60
WARN_AFTER_DAYS = 45
STALL_HOURS = 36


@dataclass(frozen=True)
class Health:
    warnings: list[str]

    @property
    def ok(self) -> bool:
        return not self.warnings


def assess(
    now: datetime,
    last_main_commit: datetime | None,
    last_watch_success: datetime | None,
    watch_state: str,
    *,
    warn_after_days: int = WARN_AFTER_DAYS,
    stall_hours: int = STALL_HOURS,
) -> Health:
    warnings: list[str] = []

    if watch_state != "active":
        warnings.append(
            f"The watch workflow is '{watch_state}', not active, so no prices are being collected. "
            "Re-enable it from the Actions tab or with: gh workflow enable watch.yml"
        )

    if last_main_commit is None:
        warnings.append("Could not read the date of the last commit to main.")
    else:
        idle = (now - last_main_commit).days
        if idle >= warn_after_days:
            left = INACTIVITY_LIMIT_DAYS - idle
            when = f"in about {left} day(s)" if left > 0 else "any time now"
            warnings.append(
                f"main has had no commits for {idle} days. GitHub disables scheduled "
                f"workflows after {INACTIVITY_LIMIT_DAYS} days without activity, so the "
                f"watcher stops {when}. Merge any PR to main to reset the clock."
            )

    if last_watch_success is None:
        warnings.append("The watch workflow has no successful run on record.")
    else:
        hours = (now - last_watch_success).total_seconds() / 3600
        if hours >= stall_hours:
            warnings.append(
                f"The watcher last succeeded {hours:.0f} hours ago, so no new prices have "
                "been checked since. Look at the latest watch runs in the Actions tab."
            )

    return Health(warnings)
