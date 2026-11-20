# Kindle dashboard: Finsbury Park trains + weather

GitHub Actions renders a 600x800 greyscale PNG every ~10 minutes and publishes it
(plus a TRMNL-style `api/display` JSON file) to GitHub Pages. The Kindle just
downloads the image. No server to run, no cost.

## Setup
1. Create a **public** GitHub repo (public repos get unlimited free Actions minutes) and push these files.
2. Get free keys:
   - National Rail: register at the National Rail Data Portal for an OpenLDBWS (Live Departure Boards) token.
   - TfL (optional but recommended): register at api-portal.tfl.gov.uk for an app key.
3. Repo -> Settings -> Secrets and variables -> Actions: add `LDBWS_TOKEN` and `TFL_APP_KEY`.
4. Repo -> Settings -> Pages -> Source: **GitHub Actions**.
5. Actions tab -> "Build Kindle dashboard" -> Run workflow. When it finishes, open
   `https://<your-username>.github.io/<repo>/` to see the image.

Local preview: `pip install -r requirements.txt && python render.py --demo`

## Notes
- GitHub pauses scheduled workflows in public repos after 60 days without repo activity. If the
  display goes stale, re-enable the workflow (or push a tiny commit).
- Rail data uses the public Huxley2 wrapper. If it's flaky, self-host it and set `HUXLEY_URL`.
- Change refresh time with the `REFRESH_SECONDS` env var (default 600).
- Layout, stations and fonts are all in `render.py`.
