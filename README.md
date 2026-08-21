# reddit-downloader

Two things in one container:

1. **The saved-posts sync** — the original behavior. Every hour it walks your own
   saved Reddit posts and downloads any media into a flat `DOWNLOAD_LOCATION`.
2. **A web UI** — search for a creator on Reddit, RedGifs or X/Twitter, preview
   their content in a grid, and download all of it or just the items you pick,
   into `downloads/<platform>/<creator>/`.

```
http://localhost:8080
```

## Quick start

```bash
cp .env.example .env      # then fill in your Reddit credentials
docker compose up -d      # remember ports: ["8080:8080"]
```

Or without Docker:

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python app.py
```

`python reddit_downloader.py` still works and still does exactly what it always
did: the sync daemon, no web server.

## Configuration

All configuration is environment variables (a `.env` file is read if present).

### Reddit (required)

| Variable | Notes |
|---|---|
| `REDDIT_CLIENT_ID` / `REDDIT_CLIENT_SECRET` | From a **script** app at https://www.reddit.com/prefs/apps |
| `REDDIT_USER_AGENT` | Anything descriptive, e.g. `my-downloader/1.0 by u/you` |
| `REDDIT_USERNAME` / `REDDIT_PASSWORD` | The account must be a developer of the app, and must not have 2FA |

RedGifs needs no credentials — the client fetches an anonymous token itself.

### X / Twitter (optional)

X has no free API read tier — new developers are metered per post read — so this
uses [gallery-dl](https://github.com/mikf/gallery-dl) against the same GraphQL
endpoints the website uses, authenticated with your own session cookie.

| Variable | Default | Notes |
|---|---|---|
| `TWITTER_AUTH_TOKEN` | *(unset)* | The `auth_token` cookie from a logged-in x.com session. Unset ⇒ X support is off |
| `TWITTER_RETWEETS` | `false` | Include media from retweets |
| `TWITTER_MAX_ITEMS` | `3000` | Cap on a "download everything" job |
| `TWITTER_MIN_INTERVAL` | `1.0` | Minimum seconds between X API calls |

To get the cookie: DevTools → Application/Storage → Cookies → `https://x.com` →
copy the value of `auth_token`.

**Treat it like a password — it *is* your session.** It expires every few weeks;
when it does, the UI says the session was rejected rather than failing silently.
Paste a fresh one and restart.

Two things about X that are different from the other platforms, both structural:

- **Search is exact-handle only.** X's user-search endpoints aren't exposed by
  gallery-dl, so typing a partial name matches nothing — type the real handle.
- **There is no item total**, so "download everything" shows an indeterminate
  progress bar rather than a percentage, and stops at `TWITTER_MAX_ITEMS`. X
  throttles deep paging hard, so an unbounded walk gets rate-limited instead of
  finishing.

gallery-dl is used for **metadata only** — it never writes a file. Downloads go
through the same `core.net` path as everything else, so the manifest, filename
scheme, cancellation and `.part` verification all behave identically.

### Storage

| Variable | Default | Notes |
|---|---|---|
| `DOWNLOAD_LOCATION` | `./downloads` | Saved posts land here, flat |
| `CREATOR_ROOT` | same as `DOWNLOAD_LOCATION` | Root for `<platform>/<creator>/` folders |
| `MIN_FREE_DISK_MB` | `2048` | A job refuses to start below this |

### Web UI

| Variable | Default | Notes |
|---|---|---|
| `WEB_ENABLED` | `true` | `false` ⇒ daemon only, same as before |
| `WEB_HOST` | `0.0.0.0` | Must stay `0.0.0.0` inside Docker or `-p` can't reach it |
| `WEB_PORT` | `8080` | |
| `WEB_THREADS` | `16` | Keep above `MAX_STREAMS` |
| `MAX_STREAMS` | `8` | Concurrent proxied media streams |

### Downloads

| Variable | Default | Notes |
|---|---|---|
| `SYNC_SAVED_ENABLED` | `true` | `false` turns off the hourly saved-posts sync |
| `TIME_BETWEEN_DOWNLOADS` | `3600` | Seconds between sync cycles |
| `DOWNLOAD_CONCURRENCY` | `3` | Parallel items within one job |
| `REDGIFS_MIN_INTERVAL` | `1.0` | Minimum seconds between RedGifs API calls |
| `CREATOR_TITLE_MAX_LEN` | `120` | Filename length budget, in bytes, creator downloads only |
| `JOB_HISTORY_LIMIT` | `50` | Finished jobs kept in memory |
| `MEDIA_PROXY_ALWAYS` | `false` | Force all media through `/api/proxy` instead of hotlinking |
| `LOG_LEVEL` | `INFO` | `TRACE` for per-file detail |

## There is no authentication

**Anyone who can reach the port can browse your library, queue downloads, and
stream media through the box.** This is intentional: it is designed to run on a
private network — a tailnet, or a LAN you trust — where the network is the
perimeter. Don't publish the port or put it behind a public reverse proxy.

Two things that are *not* access control are still enforced, and shouldn't be
removed:

- The media proxy only fetches from an allowlist of Reddit, RedGifs and X media
  hosts, and re-validates every redirect hop. Without it, `/api/proxy` is an open
  request forwarder that can reach anything the container can — cloud metadata
  endpoints, other services on the network. Note `pbs.twimg.com` and
  `video.twimg.com` are listed by *exact* host, so the rest of `twimg.com` —
  notably `abs.twimg.com`, which serves site JavaScript — stays out.
- Creator names are validated against a strict pattern and the resulting path is
  realpath-checked for containment, so a request can't write outside the download
  tree.

## How files are named

**Saved posts** keep the naming they always had, exactly:
`<sanitized title>.<ext>`, with `_<post_id>` appended on a title collision, and
galleries as `<title>_1.jpg`, `<title>_2.jpg`, … A golden test asserts this output
is byte-identical to the pre-refactor code, so nothing already on disk is renamed.

**Reddit creators** use the same scheme, with titles truncated to a 120-byte
budget — a 300-character Reddit title otherwise exceeds the 255-byte filename
limit on ext4 and fails mid-job.

**RedGifs creators** become `<YYYYMMDD>_<gifid>.mp4`. That sorts chronologically,
is globally unique, and can never end in `_<digits>`, so it can't be mistaken for
a gallery by a viewer that groups on that pattern.

**X creators** become `<YYYYMMDD>_t<tweetid>[_<text>].<ext>`, and a multi-image
tweet becomes a gallery: `..._1.jpg`, `..._2.jpg`, … The `t` before the tweet ID
is load-bearing — a tweet ID is all digits, so a bare `<date>_<id>.jpg` would end
in `_<digits>` and read as page *n* of a gallery.

Every directory carries its own `.download_manifest.json` mapping post ID → the
files it owns, so a creator folder is self-contained and portable. Delete it to
force a full re-verify of that folder.

## Layout

```
app.py                  entrypoint: web server + sync thread
reddit_downloader.py    legacy entrypoint (daemon only)
core/
  config.py             all environment variables, logging
  net.py                sessions + download primitives
  manifest.py           filename derivation + per-directory manifest
  redgifs.py            RedGifs API client
  twitter.py            X/Twitter via gallery-dl (metadata only)
  reddit_api.py         Reddit auth, search, listing, per-post dispatch
  creators.py           platform-agnostic search/listing/have-state
  jobs.py               background download jobs
  sync.py               the hourly saved-posts sync
  validate.py           input validation for anything reaching the filesystem
web/
  server.py             HTTP API + static files
  mediaproxy.py         /api/proxy, with the host allowlist
  static/               index.html, app.js, style.css (no build step)
tests/                  pytest; no network, no credentials needed
```

## API

Errors are always `{"error": {"code": ..., "message": ..., "retry_after": ...}}`.

```
GET  /api/health
GET  /api/search?q=&platform=both|reddit|redgifs|twitter
GET  /api/library
GET  /api/creators/<platform>/<name>
GET  /api/creators/<platform>/<name>/items?cursor=&limit=&sort=&kind=&only=
GET  /api/redgifs/gif/<id>
POST /api/downloads          {platform, creator, mode: "all"|"selected", ids?}
GET  /api/jobs               GET /api/jobs/<id>
POST /api/jobs/<id>/cancel   POST /api/jobs/<id>/retry-failed
GET  /api/sync               POST /api/sync/run
GET  /api/proxy?url=         allowlisted media hosts only
```

`POST /api/downloads` accepts **item IDs only, never URLs.** The server re-resolves
each ID against the source API before downloading. Accepting a client-supplied URL
would make this endpoint an arbitrary-fetch-and-write primitive.

## Tests

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests -q
```

No network calls and no credentials — both platform APIs are stubbed at the
`core.creators` seam, and downloads run against a local HTTP server. The suite
covers filename derivation (including the byte-identical golden test), manifest
persistence, path-traversal and creator-name validation, the media-proxy host
allowlist, truncated/failed downloads, and the job state machine.

## Things worth knowing

- **X cookies expire far faster than the other platforms' credentials** — weeks,
  not months. When it happens every X request fails at once, and the UI shows a
  distinct "session rejected" banner rather than a generic error, because
  retrying will never fix it.
- **Reddit caps user listings at roughly 1000 items.** "Download everything" for a
  prolific creator means "everything Reddit will list". The UI says so.
- **"Download everything" takes what the grid is filtered to**, not the whole
  catalogue: the type toggle (videos/images/galleries) and "only missing" are
  sent with the job, and the button relabels itself ("Download all images") so
  the two can't be confused. The listing is still *paged* in full — the filter
  decides what gets downloaded from it, so the caps above still apply. Under a
  filter the progress bar for RedGifs/X runs indeterminate, because the
  platform-reported total counts the unfiltered catalogue.
- **`preview.redd.it` URLs are HMAC-signed.** They are used verbatim; altering any
  query parameter turns them into 403s. If one fails to load, the frontend retries
  it once through `/api/proxy`.
- **RedGifs media is hotlinked** straight from the CDN, which currently needs no
  auth and honors range requests. If that changes, set `MEDIA_PROXY_ALWAYS=1`.
- **The RedGifs API token is bound to your egress IP and User-Agent.** If your
  outbound IP changes mid-session you'll see one `401 WrongSender`, which is
  retried transparently with a fresh token.
- **A failed download is not recorded**, so it retries on the next pass. Items that
  are genuinely deleted upstream return 404/410 and *are* recorded (under a `gone`
  key), so they aren't re-requested every hour forever.
- **Job history lives in memory.** A restart loses it; the manifests are the durable
  record, and re-running a job is idempotent.
- Creator downloads land under `DOWNLOAD_LOCATION` by default, so they show up in a
  gallery app pointed at the same tree, labelled by creator. Set `CREATOR_ROOT` to
  keep them separate.
- The image is `python:3.12-slim`. `praw` stays pinned to `~=7.8`; 8.x needs Python
  ≥3.10 so it is now installable, but the pin is deliberate — nothing here needs it.
