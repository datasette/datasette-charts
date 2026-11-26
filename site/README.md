# The Datasette Charts website

A static site: a home page, the chart gallery, the documentation and the changelog.

```bash
uv run site/build.py
```

This writes the site to `_site/`. To look at it:

```bash
python -m http.server 3473 --directory _site
```

- The documentation page is the repository's `README.md`.
- The changelog is fetched from [GitHub Releases](https://github.com/datasette/datasette-charts/releases) on every build, which fails if GitHub cannot be reached. `--offline` skips the fetch and leaves the changelog page as a link to GitHub. Set `GITHUB_TOKEN` to avoid GitHub's rate limit for anonymous requests.
- The gallery is built from `gallery.yml`. Each chart there has a source and a chart spec, which become its link to Datasette Lite and its screenshot.

## Screenshots

The screenshots in `screenshots/` are taken from a local Datasette with [shot-scraper](https://shot-scraper.datasette.io/). After changing `gallery.yml`, or the way charts are drawn:

```bash
uv run site/build.py shots
uvx shot-scraper multi site/shots.yml --retina
```

The first command writes `shots.yml` from `gallery.yml`. The second starts Datasette from `.venv` (run `uv sync` if that does not exist yet), takes every screenshot as WebP and stops it again. Add `-o site/screenshots/line-series.webp` to take just one.

Datasette needs the two demo databases in the root of the repository, with the names Datasette Lite gives them:

- `content.db` is <https://datasette.io/content.db>, which Datasette Lite loads by default.
- `data.db` has one table, `global_power_plant_database`, from the CSV file that the gallery's Datasette Lite links load:

  ```bash
  curl -LO https://raw.githubusercontent.com/wri/global-power-plant-database/master/output_database/global_power_plant_database.csv
  uvx sqlite-utils insert data.db global_power_plant_database global_power_plant_database.csv --csv
  ```

## Deployment

`.github/workflows/pages.yml` builds the site and deploys it to GitHub Pages at <https://charts.datasette.io/>. It runs on a push to `main` that changes `site/` or `README.md`, and when a release is published, so the changelog page picks up the new release notes. It does not take screenshots: the ones committed in `screenshots/` are what it publishes.
