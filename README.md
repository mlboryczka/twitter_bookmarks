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
3. Register an X developer app (https://developer.x.com); set the OAuth 2.0
   callback URL to `http://localhost:8765/callback`. Note the client ID and
   secret.
4. Get an Anthropic API key.
5. Sign up for Resend (https://resend.com), verify a sending domain, get an
   API key.
6. Clone this repo to `/opt/twitter_bookmarks`.
7. Run `deploy/bootstrap.sh` (installs Postgres 16, Caddy, uv, creates the
   `app` system user, the database user, and the database).
8. Copy `.env.example` to `.env` and fill in every value. `DIGEST_FROM_EMAIL`
   must be on a domain you've verified with Resend.
9. Run `alembic upgrade head` to apply migrations.
10. On your **local** machine, run `python scripts/run_oauth_flow.py` to
    complete the X OAuth flow and obtain a refresh token.
11. Copy the refresh token into the VPS `.env` as `X_REFRESH_TOKEN`.
12. Run `python scripts/smoke_test.py` to confirm the API auth works.
13. `systemctl enable --now twitter_bookmarks`.
14. Configure Caddy from `deploy/caddy/Caddyfile`, reload.
15. Visit `https://yourdomain.com` — the setup wizard begins.
16. Click **Begin setup**, wait for the bookmark backfill and the taxonomy
    proposal.
17. Edit the proposed taxonomy, finalize.
18. Wait for backfill classification to complete.
19. Review classifications; correct any miscategorizations.
20. Click **Complete setup**. Operating mode begins. The weekly digest will
    be emailed Monday at 8am ET.

## Re-running setup

Setup locks the taxonomy. To change categories later, run
`python scripts/reset_setup.py` — this wipes the taxonomy, classifications,
and proposals, and resets `setup_phase` to `not_started`. Visit the dashboard
and walk through the wizard again.

## Design decisions made during build

(Add any decisions made beyond the original spec here.)

## Known limitations / future work

- No semantic search.
- No multimodal classification — media URLs are captured but not interpreted.
- Single user.
- Read-only (no bookmark creation or removal).
- Taxonomy is locked after setup; to change it, re-run setup.
- No on-demand digest generation outside the scheduled weekly job (other
  than the manual `compose_digest_now.py` script).
