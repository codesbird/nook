"""
Extract all available metadata + media URLs for an Instagram post/reel/carousel
using instaloader.

CLI:
    python main.py "https://www.instagram.com/p/XXXXXXXXX/"
    python main.py "https://www.instagram.com/reel/XXXXXXXXX/" --login your_username

API:
    flask --app main run
    POST /api/instagram {"url": "https://www.instagram.com/reel/XXXXXXXXX/"}
"""

import argparse
import base64
import json
import os
import re
import sys
import tempfile
import traceback
from pathlib import Path
from typing import Any

import instaloader
from flask import Flask, jsonify, request


app = Flask(__name__)


def extract_shortcode(url: str) -> str:
    """Pull the post/reel shortcode out of a full Instagram URL."""
    match = re.search(r"instagram\.com/(?:p|reel|reels)/([A-Za-z0-9_-]+)", url)
    if not match:
        raise ValueError(f"Could not find a post/reel shortcode in URL: {url}")
    return match.group(1)


def create_instaloader() -> instaloader.Instaloader:
    return instaloader.Instaloader(
        download_pictures=False,
        download_videos=False,
        download_video_thumbnails=False,
        download_geotags=False,
        download_comments=False,
        save_metadata=False,
        compress_json=False,
    )


def load_session_from_env(loader: instaloader.Instaloader, username: str) -> bool:
    session_json_b64 = os.getenv("INSTAGRAM_SESSION_JSON_B64")
    if session_json_b64:
        session_data = json.loads(base64.b64decode(session_json_b64).decode("utf-8"))
        loader.load_session(username, session_data)
        return True

    sessionid = os.getenv("INSTAGRAM_SESSIONID")
    csrftoken = os.getenv("INSTAGRAM_CSRFTOKEN")
    if sessionid and csrftoken:
        session_data = {
            "sessionid": sessionid,
            "csrftoken": csrftoken,
            "ds_user_id": os.getenv("INSTAGRAM_DS_USER_ID", ""),
            "mid": os.getenv("INSTAGRAM_MID", ""),
            "ig_did": os.getenv("INSTAGRAM_IG_DID", ""),
            "datr": os.getenv("INSTAGRAM_DATR", ""),
            "rur": os.getenv("INSTAGRAM_RUR", ""),
        }
        loader.load_session(username, {key: value for key, value in session_data.items() if value})
        return True

    session_b64 = os.getenv("INSTAGRAM_SESSION_B64")
    if session_b64:
        session_path = Path(tempfile.gettempdir()) / f"session-{username}"
        session_path.write_bytes(base64.b64decode(session_b64))
        loader.load_session_from_file(username, filename=str(session_path))
        return True

    return False


def load_cached_session(loader: instaloader.Instaloader, username: str, session_dir: Path | None = None) -> None:
    if load_session_from_env(loader, username):
        return

    session_path = (session_dir or Path.cwd()) / f"session-{username}"
    loader.load_session_from_file(username, filename=str(session_path))


def interactive_login(loader: instaloader.Instaloader, username: str) -> None:
    session_file = Path(f"session-{username}")
    try:
        loader.load_session_from_file(username, filename=str(session_file))
        print(f"Loaded cached session for {username}")
    except FileNotFoundError:
        print(f"Logging in as {username} (you'll be prompted for a password)...")
        loader.interactive_login(username)
        loader.save_session_to_file(filename=str(session_file))
        print(f"Session cached to {session_file}")


def safe_value(source: Any, attr: str, default: Any = None) -> Any:
    try:
        return getattr(source, attr)
    except Exception:
        return default


def build_post_dict(post: instaloader.Post) -> dict:
    """
    Pull together the useful fields instaloader exposes on a Post object.
    Some media URL fields are lazy and may fail if Instagram returns partial data,
    so optional fields are read defensively.
    """
    is_video = bool(safe_value(post, "is_video", False))
    typename = safe_value(post, "typename")
    date_utc = safe_value(post, "date_utc")
    location = safe_value(post, "location")
    post_url = safe_value(post, "url")
    post_video_url = safe_value(post, "video_url") if is_video else None

    data = {
        "shortcode": safe_value(post, "shortcode"),
        "post_id": safe_value(post, "mediaid"),
        "type": typename,
        "is_video": is_video,
        "title": safe_value(post, "title"),
        "caption": safe_value(post, "caption"),
        "caption_hashtags": safe_value(post, "caption_hashtags", []),
        "caption_mentions": safe_value(post, "caption_mentions", []),
        "owner_username": safe_value(post, "owner_username"),
        "owner_id": safe_value(post, "owner_id"),
        "date_utc": date_utc.isoformat() if date_utc else None,
        "likes": safe_value(post, "likes"),
        "comments": safe_value(post, "comments"),
        "video_view_count": safe_value(post, "video_view_count") if is_video else None,
        "video_duration": safe_value(post, "video_duration") if is_video else None,
        "location": str(location) if location else None,
        "is_sponsored": safe_value(post, "is_sponsored"),
        "accessibility_caption": safe_value(post, "accessibility_caption"),
        "url": post_url,
        "video_url": post_video_url,
        "slides": [],
    }

    if typename == "GraphSidecar":
        try:
            nodes = list(post.get_sidecar_nodes())
        except Exception as exc:
            data["slides_error"] = str(exc)
            nodes = []

        for index, node in enumerate(nodes):
            node_is_video = bool(safe_value(node, "is_video", False))
            data["slides"].append(
                {
                    "index": index,
                    "is_video": node_is_video,
                    "display_url": safe_value(node, "display_url"),
                    "video_url": safe_value(node, "video_url") if node_is_video else None,
                }
            )
    else:
        data["slides"].append(
            {
                "index": 0,
                "is_video": is_video,
                "display_url": post_url,
                "video_url": post_video_url,
            }
        )

    return data


def fetch_instagram_metadata(url: str, login: str | None = None, allow_interactive_login: bool = False) -> dict:
    url = url.strip()
    shortcode = extract_shortcode(url)
    loader = create_instaloader()

    username = login or os.getenv("INSTAGRAM_USERNAME")
    if username:
        if allow_interactive_login:
            interactive_login(loader, username)
        else:
            session_dir = Path(os.getenv("INSTAGRAM_SESSION_DIR", "."))
            load_cached_session(loader, username, session_dir=session_dir)

    post = instaloader.Post.from_shortcode(loader.context, shortcode)
    return build_post_dict(post)


@app.get("/")
def health() -> tuple[dict, int]:
    return {"ok": True, "service": "instagram-metadata-api"}, 200


@app.get("/api/debug")
def debug_config():
    return jsonify(
        {
            "ok": True,
            "has_instagram_username": bool(os.getenv("INSTAGRAM_USERNAME")),
            "has_instagram_session_b64": bool(os.getenv("INSTAGRAM_SESSION_B64")),
            "has_instagram_session_json_b64": bool(os.getenv("INSTAGRAM_SESSION_JSON_B64")),
            "has_instagram_sessionid": bool(os.getenv("INSTAGRAM_SESSIONID")),
            "has_instagram_csrftoken": bool(os.getenv("INSTAGRAM_CSRFTOKEN")),
            "instaloader_version": getattr(instaloader, "__version__", None),
        }
    )


@app.post("/api/instagram")
@app.post("/api/metadata")
def instagram_metadata_endpoint():
    payload = request.get_json(silent=True) or {}
    url = payload.get("url")
    login = payload.get("login")

    if not url:
        return jsonify({"error": "Missing required parameter: url"}), 400

    try:
        data = fetch_instagram_metadata(url, login=login, allow_interactive_login=False)
    except FileNotFoundError:
        return (
            jsonify(
                {
                    "error": "Instagram session file was not found for the requested login.",
                    "hint": "Set INSTAGRAM_USERNAME plus either INSTAGRAM_SESSION_JSON_B64 or INSTAGRAM_SESSIONID and INSTAGRAM_CSRFTOKEN in Vercel, or call without login for public posts.",
                }
            ),
            401,
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        response = {
            "error": "Failed to fetch Instagram metadata.",
            "details": str(exc),
            "exception_type": type(exc).__name__,
            "hint": "Instagram often blocks anonymous requests from Vercel. Add a cached Instagram session with INSTAGRAM_USERNAME plus INSTAGRAM_SESSION_JSON_B64, or INSTAGRAM_SESSIONID and INSTAGRAM_CSRFTOKEN.",
        }
        if os.getenv("DEBUG_ERRORS") == "1":
            response["traceback"] = traceback.format_exc().splitlines()[-12:]
        return jsonify(response), 502

    return jsonify(data), 200


def main() -> None:
    parser = argparse.ArgumentParser(description="Dump full Instagram post/reel metadata to JSON via instaloader.")
    parser.add_argument("url", help="Instagram post/reel URL")
    parser.add_argument(
        "--login",
        help="Your Instagram username, to log in for private posts / to avoid rate limits.",
        default=None,
    )
    parser.add_argument(
        "-o",
        "--output",
        help="Output JSON file path (default: instagram_data.json in current dir).",
        default="instagram_data.json",
    )
    args = parser.parse_args()

    try:
        data = fetch_instagram_metadata(args.url, login=args.login, allow_interactive_login=True)
    except Exception as exc:
        print(f"ERROR: Failed to fetch post. {exc}")
        print("If this is a private post, or you're being rate-limited, try again with --login your_username")
        sys.exit(1)

    out_path = Path(args.output)
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str, ensure_ascii=False)

    print(f"\nSaved full metadata JSON to: {out_path.resolve()}")
    print("\n=== KEY METADATA ===")
    print(f"Type:         {data['type']} ({'video' if data['is_video'] else 'image/carousel'})")
    print(f"Owner:        {data['owner_username']}")
    print(f"Date:         {data['date_utc']}")
    print(f"Likes:        {data['likes']}")
    print(f"Comments:     {data['comments']}")
    print(f"Caption:      {(data['caption'] or '')[:200]}")

    print(f"\n=== SLIDES ({len(data['slides'])}) ===")
    for slide in data["slides"]:
        kind = "VIDEO" if slide["is_video"] else "IMAGE"
        media_url = slide["video_url"] if slide["is_video"] else slide["display_url"]
        print(f"  Slide {slide['index']}: {kind} -> {media_url}")


if __name__ == "__main__":
    main()



