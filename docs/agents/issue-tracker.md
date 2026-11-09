# Issue tracker: Forgejo

Issues and specs for this repo live as Forgejo issues at
`https://git.serrebiradio.com/serrebi/blindDL/issues`.
There is no `tea`/`gh` equivalent wired up; use the Forgejo API directly
(`curl` below; PowerShell `Invoke-RestMethod` works the same).

All calls share this base and header:

- Base: `https://git.serrebiradio.com/api/v1/repos/serrebi/blindDL`
- Header: `Authorization: token $FORGEJO_TOKEN`

Reads are public and work without the header. Writes need a token with
issue scope — the release token in `/root/forgejo-muse-token.txt` lacks it
(`read:issue` error), so mint one with issue read/write for tracker work.
Old GitHub issues stay on GitHub; synced ones carry a
`forgejo-github-sync` footer in the body.

## Conventions

- **Create an issue**: `POST /issues` with `{"title": "...", "body": "...",
  "labels": [<label ids>]}`. Labels take numeric IDs, not names; see the
  table below.
- **Read an issue**: `GET /issues/<number>` (`number`, `title`, `body`,
  `state`, `labels[].name`, `comments` count).
- **List issues**: `GET /issues?state=open|closed|all`, then keep entries
  whose `pull_request` field is null to exclude pull requests.
- **Comment on an issue**: `POST /issues/<number>/comments` with
  `{"body": "..."}`. Read them with `GET /issues/<number>/comments`.
- **Apply / remove labels**: `PUT /issues/<number>/labels` with
  `{"labels": [<full id set>]}` (it replaces, so GET first and merge).
- **Close**: `PATCH /issues/<number>` with `{"state": "closed"}`.
- **Assign**: `PATCH /issues/<number>` with `{"assignees": ["<login>"]}`.

Label IDs (stable per repo; re-list with `GET /labels` if one 404s):

| Label | ID |
| `needs-triage` | 558 |
| `needs-info` | 557 |
| `ready-for-agent` | 559 |
| `ready-for-human` | 560 |
| `wontfix` | 366 |

Example (Windows `curl.exe`):

    curl -s https://git.serrebiradio.com/api/v1/repos/serrebi/blindDL/issues?state=open

## Pull requests as a triage surface

**PRs as a request surface: no.** _(Set to `yes` if this repo treats external PRs as feature requests; `/triage` reads this flag.)_

When set to `yes`, PRs run through the same labels and states as issues:

- **Read a PR**: `GET /pulls/<number>`; the `head` object names the branch
  (`head.ref`) and its repo (`head.repo.clone_url`).
- **Diff a PR**: `git fetch <head clone_url> <head ref>` into a scratch
  branch and diff locally.
- **List open PRs**: `GET /pulls?state=open`.
- **Comment / label / close**: the issue endpoints above work on PRs too
  (`POST /issues/<number>/comments`, `PUT /issues/<number>/labels`,
  `PATCH /issues/<number>` with `{"state": "closed"}`).
- **Merge**: `POST /pulls/<number>/merge` with `{"Do": "squash"}`.

Forgejo shares one number space across issues and PRs, so a bare `#42` may
be either: `GET /issues/42` carries a `pull_request` object on PRs and
`null` on plain issues.

## When a skill says "publish to the issue tracker"

Create a Forgejo issue.

## When a skill says "fetch the relevant ticket"

Read it as in **Read an issue** above.

## Wayfinding operations

Used by `/wayfinder`. The **map** is a single issue with **child** issues as tickets. Forgejo has no sub-issues or native dependencies, so links live in issue bodies.

- **Map**: a single issue holding the Notes / Decisions-so-far / Fog body, with a task list linking each child. First create its label with `POST /labels {"name": "wayfinder:map", "color": "0e8a16"}`, then create the issue with that label id.
- **Child ticket**: an issue with `Part of #<map>` at the top of its body, listed in the map's task list. Labels: `wayfinder:<type>` (`research`/`prototype`/`grilling`/`task`); create each with `POST /labels` before first use. Once claimed, the ticket is assigned to the driving dev.
- **Blocking**: a `Blocked by: #<n>, #<n>` line at the top of the child body, the canonical representation. A ticket is unblocked when every blocker is closed.
- **Frontier query**: list the map's open children (task list in the map body), drop any with an open issue in its `Blocked by` line or an assignee; first in map order wins.
- **Claim**: `PATCH /issues/<n>` with `{"assignees": ["<your login>"]}`, the session's first write.
- **Resolve**: `POST /issues/<n>/comments` with the answer, then `PATCH /issues/<n>` with `{"state": "closed"}`, then append a context pointer to the map body (`PATCH /issues/<map>` with the full new `body`; GET first since it replaces).
