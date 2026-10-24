# IT Vibe Content Editor — Codex handoff

## Goal

Deploy the existing Telegram content-editor MVP to the Ubuntu server at
`76.13.227.36` as `root`, without disrupting unrelated services.

The deployment target is `/opt/itvibe-content-editor`. The service must run with
Docker Compose and restart automatically.

## Current state

- The Python MVP is implemented.
- Solidot RSS ingestion, source extraction, Qwen triage/writing, SQLite storage,
  Telegram editorial actions, media workflow and publishing are present.
- The repository contains Docker and Compose configuration.
- The one-command macOS deployment script is
  `scripts/deploy_remote.sh`.
- The deployment script uploads the complete Git repository, installs Docker if
  required, builds the test image, starts the runtime container and prints logs.
- No credentials are committed to Git.

## Git history

- `8677d74` — one-command Ubuntu deployment
- `62fe2d0` — media workflow and deployment checks
- `2d93c55` — formatting
- `7dd6b8b` — initial MVP

## Required secrets

Obtain these interactively from the user or let the user enter them directly in
the terminal. Never write them to Git or display them in logs.

- Ubuntu root password
- Telegram bot token for `@itvibe_editor_bot`
- Alibaba Cloud Model Studio / Qwen API key
- Qwen OpenAI-compatible base URL; the deployment script defaults to the
  international DashScope endpoint

Credentials previously pasted into chat must be treated as temporary and should
be rotated after verification.

## Deployment procedure

1. Inspect the current server state first:
   - Ubuntu version and free disk space
   - existing Docker containers and Compose projects
   - `/opt/itvibe-content-editor`, if already present
2. Do not stop, remove or reconfigure unrelated containers.
3. Run from the repository root:

   ```bash
   ./scripts/deploy_remote.sh root@76.13.227.36
   ```

4. Verify on the server:

   ```bash
   cd /opt/itvibe-content-editor
   git status --short
   git log -1 --oneline
   docker compose ps
   docker compose logs --tail=150 editor
   ```

5. Confirm that the container is healthy/running and that Telegram polling has
   started without authentication or Qwen model errors.
6. Ask the user to open `@itvibe_editor_bot`, send `/start`, then `/setup`.
7. Verify a forwarded Telegram post and one Solidot RSS item end to end.

## Important behavior

- The first Telegram user to send `/start` on a clean database becomes the bot
  owner, so the user must do this immediately after launch.
- The bot must be an administrator of both the editorial channel and the final
  publishing channel.
- Posts are limited to 2,000 visible characters.
- Posts use Telegram rich messages so the selected image and the complete text
  remain in one editable message. A classic media caption is only a fallback
  for posts of up to 1,024 characters.
- Solidot and known secondary publishers are discovery sources only and are
  never rendered as links in post text.
- Images are taken from linked articles first. If none are available, Qwen's
  `web_search_image` tool selects a contextual editorial illustration and the
  pipeline verifies that it is directly downloadable before creating a draft.
- Telegram channel avatars and author profile pictures are excluded while parsing
  public channel pages. If a Telegram post has no actual content image, the pipeline
  falls through to linked-article media and then contextual image search.
- Automatic stories are skipped for missing media only if both image strategies fail.
- Editorial posts must contain no emoji and exactly three hashtags on one line.
  Hashtag-only lines accidentally emitted inside the body are removed, including
  duplicate hashtag paragraphs recovered from native Telegram edits.
- Chinese ideographs are forbidden in the final Russian title, body and hashtags.
  Validation sends mixed-language model output through repair and final rendering
  blocks any draft that still contains ideographs.
- Rich-message titles use ordinary bold text instead of heading typography, and every
  pair of body paragraphs is separated by exactly one blank line.
- Triage treats space missions, significant device-market shifts, major operating-system
  changes and credible longevity technology or research as editorially relevant.
- Industry-critical events such as urgent security fixes, mandatory upgrades, widespread
  outages and breaking changes in popular infrastructure bypass the normal relevance
  threshold and receive a leading `⚡️` marker in the editorial post title.
- RSS and Telegram polling is adaptive by default: every five minutes at
  05:00–23:00 Samara time, and every 30 minutes outside that window.
- Telegram posts containing at least three linked, numbered news items are handled as
  digests. Their item order and URL mapping are preserved, and each item is rewritten
  briefly in Russian with its original destination embedded in the text.
- Plain text and forwarded posts submitted directly to the editorial channel are always
  prepared regardless of the normal relevance threshold. They first pass an independent
  Qwen web fact-check and a second evidence-only pass over the discovered pages or search
  excerpts. The second pass receives only pages returned by that independent search, never
  technical resources extracted from Telegram. Current UTC date is supplied explicitly, and
  404 responses or missing homepage announcements cannot be treated as proof that a claim is
  false. The writer receives only verified facts, uncertainties and linkable primary sources,
  not the unverified forwarded text. Editorial groups are supported too. The bot posts an
  immediate temporary acceptance status and removes it when the draft is delivered. Existing
  drafts and bot interface messages are ignored by this route. This behavior was verified on
  2026-10-23 with draft `#87`. Editorial filler such as «Это важный сигнал для
  отрасли» is rejected and removed deterministically.
- Ordinary unpublished drafts are retained in SQLite for 30 days from creation and then
  removed by a daily cleanup task. Scheduled drafts and drafts with any successful
  publication are preserved even if their aggregate status is inconsistent. Cleanup never
  edits or deletes Telegram messages. Content-sanitizing feature commit `162558b`,
  124 tests passing.

## Completion criteria

Deployment is complete only after all of the following are verified:

- repository exists at `/opt/itvibe-content-editor` with Git history;
- Docker test stage passes;
- `itvibe-content-editor` container remains running;
- logs contain no missing-environment-variable, Telegram authentication, or
  immediate Qwen endpoint/model errors;
- `/start` and `/setup` work in Telegram;
- an editorial draft can be created and approved for publication.
