# Hanime Helper

A private helper API for searching Hanime.tv and reading the owner's authenticated account pages. This branch is deployed separately from the original MAL Helper.

## Current capabilities

- Search Hanime videos
- Retrieve video and franchise details
- Connect one Hanime.tv account
- Check connection status
- Probe authenticated account pages
- Read favorites, watch history, and playlists when those pages are available to the signed-in account
- Disconnect and delete the stored session

Account-page scraping is unofficial and may stop working if Hanime changes its website. The favorites, history, and playlist routes must be verified against the owner's signed-in account before relying on them.

## Security

- The Hanime password is submitted to the helper only during the sign-in request and is not stored.
- Session cookies and API session tokens are encrypted at rest with Fernet.
- The encryption key is held only in Render environment variables.
- Never commit the helper API key, database URL, encryption key, passwords, or session cookies to GitHub.
- Never paste passwords or session cookies into ChatGPT.

## Render environment variables

Set these in the `himel106j-hanime-sync` Render service:

- `APP_BASE_URL` — the deployed service URL
- `HELPER_API_KEY` — a long random secret used by the ChatGPT Action
- `DATABASE_URL` — the private Neon Postgres connection string
- `HANIME_SESSION_ENCRYPTION_KEY` — a Fernet key stored only in Render secrets
- `HANIME_BASE_URL` — optional; defaults to `https://hanime.tv`
- `HANIME_SEARCH_URL` — optional; defaults to Hanime's search service
- `HANIME_RAPI_BASE` — optional; defaults to the current CDN API host

## Connect the account

After the service is deployed, open the service's `/connect` page in a browser. Enter the helper API key and Hanime credentials directly on that HTTPS page. Do not send credentials in ChatGPT. The service tries the website login and supported Hanime session API methods, then stores only the resulting encrypted session.

## Verify the account pages

After connecting, use the ChatGPT Action's `/me/status` and `/me/probe` operations. The probe reports whether Hanime's authenticated account pages such as favorites, history, and playlists are accessible. Only use the individual list endpoints after the probe confirms the relevant pages.

## ChatGPT Action

Import `openapi.yaml` into an Action and configure API Key authentication:

- Header name: `X-API-Key`
- API key value: the same `HELPER_API_KEY` stored in Render
- Privacy policy: `https://himel106j-hanime-sync.onrender.com/privacy`

## Health check

`https://himel106j-hanime-sync.onrender.com/health`

