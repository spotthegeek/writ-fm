# Program Schedule

This file mirrors the checked-in default schedule in `config/schedule.yaml`.

Current station defaults:

- Station name: `Crouch-FM`
- Timezone: `Australia/Adelaide`
- Weekly overrides: none

All times below are local station time and repeat every day unless `schedule.overrides` is populated.

## Daily Base Schedule

- `00:00-03:00` — `/r/alien_theory`
- `03:00-06:00` — `/r/nosleep`
- `06:00-09:00` — `/r/sysadmin`
- `09:00-12:00` — `YouTube AI`
- `12:00-15:00` — `/r/alien_theory`
- `15:00-18:00` — `/r/nosleep`
- `18:00-21:00` — `/r/sysadmin`
- `21:00-00:00` — `YouTube AI`

## Show Notes

- `alien_theory`
  Uses Reddit subreddit sources such as `/r/Alien_Theory`, `/r/EBEs`, and `/r/UFOs`, primarily routed to `reddit_post`.

- `nosleep`
  Uses `/r/nosleep` and routes to `reddit_storytelling`.

- `sysadmin`
  Uses `/r/sysadmin` and adjacent infrastructure discussion as `reddit_post`.

- `youtube-ai`
  Pulls from configured AI-related YouTube channels and routes to the `youtube` segment type.

## Defined But Not Scheduled

These shows still exist in `config/schedule.yaml` but are not in the current base rotation:

- `signal_report`
- `crosswire`
- `listener_hours`

If those are meant to return, add them back through the admin UI or directly in `config/schedule.yaml`.
