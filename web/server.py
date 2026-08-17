"""The web API and static file server.

There is no access control here. This is meant to be reachable only over a
private network (a tailnet, or a LAN you trust) - do not publish the port.

Two protections that are NOT access control and are still in force: the media
proxy's host allowlist (web/mediaproxy.py), which stops the server being used as
a request forwarder, and creator-name validation (core/validate.py), which keeps
downloads inside the download tree.
"""
import functools
import os

import prawcore
from flask import Flask, Response, jsonify, request, send_from_directory

from core import config, creators, jobs, sync
from core.config import logger
from core.validate import (ValidationError, validate_creator, validate_item_id,
                           validate_platform)
from web import mediaproxy

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


def _error(code, message, status=400, **extra):
    payload = {"error": dict({"code": code, "message": message}, **extra)}
    return jsonify(payload), status


def handle_errors(fn):
    """Turn the exceptions our core layer raises into API error responses."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ValidationError as e:
            return _error(e.code, e.message, 400)
        except jobs.JobError as e:
            return _error(e.code, e.message, e.status)
        except mediaproxy.ProxyError as e:
            return _error(e.code, e.message, e.status)
        except creators.CreatorUnavailable as e:
            return _error("creator_not_found",
                          "No such %s creator: %s" % (e.platform, e.name), 404)
        except prawcore.exceptions.TooManyRequests as e:
            retry = getattr(e, "retry_after", None)
            return _error("rate_limited", "Reddit is rate limiting us.", 429,
                          retry_after=retry)
        except prawcore.exceptions.NotFound:
            return _error("creator_not_found", "That creator does not exist.", 404)
        except prawcore.exceptions.PrawcoreException as e:
            logger.error("Reddit error on %s: %s", request.path, e)
            return _error("upstream_error", "Reddit is unavailable right now.", 502)
        except Exception as e:  # noqa: BLE001
            logger.exception("Unhandled error on %s: %s", request.path, e)
            return _error("server_error", "Something went wrong.", 500)
    return wrapper


def create_app(manager=None):
    app = Flask(__name__, static_folder=None)
    manager = manager or jobs.get_manager()

    @app.after_request
    def security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        return response

    # --- static -----------------------------------------------------------

    @app.route("/")
    def index():
        response = send_from_directory(STATIC_DIR, "index.html")
        response.headers["Cache-Control"] = "no-cache"
        return response

    @app.route("/static/<path:filename>")
    def static_files(filename):
        return send_from_directory(STATIC_DIR, filename, conditional=True)

    # --- status -----------------------------------------------------------

    @app.route("/api/health")
    @handle_errors
    def health():
        return jsonify({
            "ok": True,
            "ffmpeg": config.FFMPEG_AVAILABLE,
            "sync_enabled": config.SYNC_SAVED_ENABLED,
            "concurrency": manager.concurrency,
            "nsfw_blur": True,
            "media_proxy_always": config.MEDIA_PROXY_ALWAYS,
            "listing_cap": creators.REDDIT_LISTING_CAP,
            "warnings": config.startup_warnings(),
        })

    @app.route("/api/sync")
    @handle_errors
    def sync_status():
        return jsonify(sync.get_state())

    @app.route("/api/sync/run", methods=["POST"])
    @handle_errors
    def sync_run():
        if not config.SYNC_SAVED_ENABLED:
            return _error("sync_disabled", "The saved-posts sync is disabled.", 403)
        if sync.get_state().get("running"):
            return _error("already_running", "A sync is already in progress.", 409)
        sync.WAKE.set()
        return jsonify({"ok": True}), 202

    # --- search and browse ------------------------------------------------

    @app.route("/api/search")
    @handle_errors
    def search():
        query = (request.args.get("q") or "").strip()
        if len(query) < 2:
            return _error("invalid_query", "Enter at least two characters.")
        if len(query) > 64:
            return _error("invalid_query", "That search is too long.")
        platform = request.args.get("platform", "both")
        if platform not in ("both", "reddit", "redgifs"):
            return _error("invalid_platform", "Unknown platform.")
        limit = _clamp(request.args.get("limit"), 25, 1, 50)
        return jsonify(creators.search(query, platform=platform, limit=limit))

    @app.route("/api/library")
    @handle_errors
    def library():
        return jsonify({"creators": creators.local_library()})

    @app.route("/api/creators/<platform>/<name>")
    @handle_errors
    def creator(platform, name):
        # Validated here as well as deeper down: the route owns its path params,
        # and this is the boundary a traversal attempt actually arrives at.
        platform = validate_platform(platform)
        name = validate_creator(platform, name)
        info = creators.get_creator(platform, name)
        if info["profile"] is None:
            # Still useful if we have local files for them.
            if not info["have"]["files"]:
                return _error("creator_not_found", "That creator was not found.", 404)
        return jsonify(info)

    @app.route("/api/creators/<platform>/<name>/items")
    @handle_errors
    def creator_items(platform, name):
        platform = validate_platform(platform)
        name = validate_creator(platform, name)
        return jsonify(creators.list_items(
            platform, name,
            cursor=request.args.get("cursor") or None,
            limit=_clamp(request.args.get("limit"), 30, 1, 100),
            sort=request.args.get("sort", "new"),
            kind=request.args.get("kind", "all"),
            only=request.args.get("only", "all"),
        ))

    @app.route("/api/redgifs/gif/<gif_id>")
    @handle_errors
    def redgifs_gif(gif_id):
        media = creators.resolve_redgifs_media(validate_item_id("redgifs", gif_id))
        if not media:
            return _error("gone_upstream", "That gif is no longer available.", 410)
        return jsonify(media)

    # --- jobs -------------------------------------------------------------

    @app.route("/api/downloads", methods=["POST"])
    @handle_errors
    def create_download():
        payload = request.get_json(silent=True) or {}
        job = manager.submit(
            payload.get("platform"),
            payload.get("creator"),
            payload.get("mode", "selected"),
            payload.get("ids"),
        )
        return jsonify({"job": job}), 202

    @app.route("/api/jobs")
    @handle_errors
    def list_jobs():
        return jsonify({
            "jobs": manager.list(limit=_clamp(request.args.get("limit"), 20, 1, 100)),
            "active": manager.active_count(),
        })

    @app.route("/api/jobs/<job_id>")
    @handle_errors
    def get_job(job_id):
        job = manager.get(job_id)
        if job is None:
            return _error("not_found", "No such job.", 404)
        return jsonify({"job": job})

    @app.route("/api/jobs/<job_id>/cancel", methods=["POST"])
    @handle_errors
    def cancel_job(job_id):
        return jsonify({"job": manager.cancel(job_id)}), 202

    @app.route("/api/jobs/<job_id>/retry-failed", methods=["POST"])
    @handle_errors
    def retry_job(job_id):
        return jsonify({"job": manager.retry_failed(job_id)}), 202

    # --- media proxy ------------------------------------------------------

    @app.route("/api/proxy")
    @handle_errors
    def proxy():
        url = request.args.get("url")
        upstream, headers, status = mediaproxy.open_stream(
            url,
            range_header=request.headers.get("Range"),
            if_none_match=request.headers.get("If-None-Match"),
        )
        if upstream is None:
            return Response(status=status, headers=headers)
        return Response(mediaproxy.iter_body(upstream), status=status, headers=headers)

    @app.errorhandler(404)
    def not_found(_e):
        if request.path.startswith("/api/"):
            return _error("not_found", "No such endpoint.", 404)
        # Any other path is a client-side route; let the SPA handle it.
        response = send_from_directory(STATIC_DIR, "index.html")
        response.headers["Cache-Control"] = "no-cache"
        return response

    @app.errorhandler(405)
    def bad_method(_e):
        return _error("method_not_allowed", "That method is not allowed here.", 405)

    return app


def _clamp(raw, default, low, high):
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, value))
