# MAL Helper

A tiny personal API that lets a Custom GPT manage your MyAnimeList account.

## What it can do

- Search anime by title
- Read anime details
- Add anime to your MAL list
- Change status: Watching / Completed / On Hold / Dropped / Plan to Watch
- Update watched episode count
- Set a score
- Remove anime from your list
- Read your MAL list

## Important security rules

Never put these values in GitHub:
- MAL client secret
- HELPER_API_KEY
- MAL access/refresh tokens

Keep them only in your hosting provider's environment/secrets settings.

## Files

- `app.py` - FastAPI helper server
- `requirements.txt` - Python packages
- `.env.example` - environment variable names only
- `openapi.yaml` - schema to paste into your Custom GPT Action
- `.gitignore` - keeps local secrets out of GitHub

## Environment variables

Set these on your hosting provider:

- `MAL_CLIENT_ID`
- `MAL_CLIENT_SECRET`
- `APP_BASE_URL`
- `HELPER_API_KEY`
- `TOKEN_DB_PATH` (optional; point this to persistent storage)

## MAL redirect URL

After deployment, your MAL API app's redirect URL must be:

`https://YOUR-DEPLOYED-DOMAIN/auth/callback`

Replace the placeholder with your real helper domain.

## First authorization

After deployment and environment variables are set, open:

`https://YOUR-DEPLOYED-DOMAIN/auth/start`

Approve access on MyAnimeList. When you see `Connected ✅`, the helper has stored your token.

## Custom GPT Action

1. Open your GPT editor.
2. Create a new Action.
3. Authentication: API Key.
4. Put your `HELPER_API_KEY` in the action's API key setting.
5. Set the header name to `X-API-Key`.
6. Open `openapi.yaml`.
7. Replace `https://YOUR-DEPLOYED-DOMAIN.example.com` with your real helper URL.
8. Paste the schema into the Action editor.
9. Privacy policy URL: `https://YOUR-DEPLOYED-DOMAIN/privacy`.

## Local test

```bash
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8000
```

For local testing, set `APP_BASE_URL=http://localhost:8000`.
