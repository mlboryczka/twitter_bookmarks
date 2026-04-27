# twitter_bookmarks

Personal X (Twitter) bookmark categorization and weekly digest. Pulls your
bookmarks via the X API, has Claude propose a personalized taxonomy based on
what you actually save, classifies every bookmark, and emails you a weekly
synthesis grouped by category.

Single-user. Self-hosted on a VPS. Read-only against X.

## How it works

Two operational phases share the same web UI:

1. **Setup** (run once): backfill all your bookmarks, let Claude Sonnet propose
   a taxonomy, edit the proposal, finalize, classify the backfill with Claude
   Haiku, review and correct any miscategorizations.
2. **Operating mode** (forever): daily incremental bookmark pull and
   classification, weekly Monday-morning digest emailed via Resend. Every
   correction you make becomes a few-shot example for the next classifier
   call.

## Bootstrap

Below is the deploy guide for a fresh Ubuntu 24.04 VPS.

1. Spin up an Ubuntu 24.04 VPS.
2. Point a domain at the VPS IP (A record).
3. Register an X developer app at https://developer.x.com. Set the OAuth
   2.0 callback URL to `http://localhost:8765/callback` (for the local
   OAuth flow). Note the client ID and secret. Look up your numeric
   user ID — the bookmarks endpoint requires it.
4. Get an Anthropic API key (https://console.anthropic.com).
5. Sign up for Resend (https://resend.com), verify a sending domain,
   and get an API key.
6. Clone this repo to `/opt/twitter_bookmarks`.
7. As root, run `bash deploy/bootstrap.sh`. This installs Postgres 16,
   Caddy, and uv, creates the `app` system user, and creates the database
   user + database. Note the printed `DATABASE_URL`.
8. As `app`, copy `.env.example` to `.env` and fill in every value.
   `DIGEST_FROM_EMAIL` must be on a domain you've verified with Resend.
9. Run `cd /opt/twitter_bookmarks && uv sync && uv run alembic upgrade head`.
10. On your **local** machine (with the same X_CLIENT_ID/SECRET in a local
    `.env`), run `uv run python scripts/run_oauth_flow.py`. A browser
    window opens; complete the consent flow. The script prints the
    refresh token.
11. Copy the refresh token into the VPS `.env` as `X_REFRESH_TOKEN`.
12. On the VPS, run `uv run python scripts/smoke_test.py` to confirm
    auth works (it should print your @username and confirm an `api_calls`
    row was written).
13. Install the systemd unit:
    `sudo cp deploy/systemd/twitter_bookmarks.service /etc/systemd/system/`
    then `sudo systemctl daemon-reload && sudo systemctl enable --now twitter_bookmarks`.
14. Edit `deploy/caddy/Caddyfile` to use your real domain, then
    `sudo cp deploy/caddy/Caddyfile /etc/caddy/Caddyfile && sudo systemctl reload caddy`.
15. Visit `https://yourdomain.com`. Sign in with your dashboard
    credentials (Basic Auth, from `.env`). The setup wizard begins.
16. Click **Begin setup**. Wait for the bookmark backfill (a few minutes)
    and the taxonomy proposal.
17. Edit the proposed taxonomy — rename, merge, split, delete, or add
    categories — then finalize.
18. Wait for backfill classification to complete.
19. Review classifications; correct any miscategorizations from the
    review screen.
20. Click **Complete setup**. Operating mode begins. The weekly digest
    will be emailed every Monday at 8am ET.

## Re-running setup

Setup locks the taxonomy. To change categories later, run
`python scripts/reset_setup.py` — this wipes the taxonomy, classifications,
and proposals, and resets `setup_phase` to `not_started`. Visit the dashboard
and walk through the wizard again.

## Design decisions made during build

- **`bookmarks.has_full_thread` is a "ready-to-classify" flag, not a
  multi-tweet indicator.** The thread reconstructor always writes a
  `bookmark_threads` row (single-tweet or multi-tweet) and sets
  `has_full_thread=true` when reconstruction has been attempted. This way
  the classifier worker can use a single predicate to find bookmarks
  whose text is finalized. `thread_root_id` is only set when there's an
  actual multi-tweet chain.
- **Bookmark `bookmarked_at` defaults to ingestion time.** The X
  bookmarks endpoint does not return a per-bookmark timestamp, so we
  approximate with the wall-clock at ingestion. For the backfill, all
  bookmarks pulled in the same backfill batch share the same timestamp;
  ordering within a backfill is preserved by the X API's "most recent
  first" page order via the `bookmark_threads` reconstruction order. The
  weekly digest's "bookmarks added this week" filter therefore really
  means "bookmarks ingested this week."
- **`x_api/client.request` does not auto-log on success.** Endpoint
  wrappers compute cost from the response payload and log a single row
  to `api_calls` per request. Only error responses are auto-logged from
  the client.
- **Basic Auth is enforced in FastAPI, not in Caddy.** The spec
  mentions both `DASHBOARD_USERNAME`/`DASHBOARD_PASSWORD` env vars and
  Caddy basicauth. Putting it at both layers means logging in twice
  per browser session and keeping bcrypt hashes in two places. The
  FastAPI `Depends(require_basic_auth)` covers every route already, so
  the Caddyfile is a plain reverse proxy.

## Known limitations / future work

- No semantic search.
- No multimodal classification — media URLs are captured but not interpreted.
- Single user.
- Read-only (no bookmark creation or removal).
- Taxonomy is locked after setup; to change it, re-run setup.
- No on-demand digest generation outside the scheduled weekly job (other
  than the manual `compose_digest_now.py` script).
