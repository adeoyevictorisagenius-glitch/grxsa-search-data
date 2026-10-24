
import json
import os
import uuid
from datetime import datetime, timezone

import firebase_admin
from firebase_admin import credentials, firestore
import turso_serverless


def required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def as_int(value, default=0) -> int:
    try:
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, (list, tuple, set, dict)):
            return len(value)
        return int(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def epoch_seconds(value) -> int:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return 0
    else:
        return 0

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    return int(dt.timestamp())


def first_image_url(images) -> str:
    if not isinstance(images, list):
        return ""

    for image in images:
        if isinstance(image, dict) and image.get("url"):
            return str(image["url"])

    return ""


def normalize_video(doc, run_id):
    data = doc.to_dict() or {}

    cost = as_int(data.get("cost"))
    likes = data.get("likes") or []
    views = data.get("views", 0)

    return {
        "content_id": f"video:{doc.id}",
        "source_id": doc.id,
        "content_type": "video",
        "creator_id": str(data.get("userId") or ""),
        "title": str(data.get("title") or ""),
        "description": str(data.get("description") or ""),
        "media_url": str(data.get("videoUrl") or ""),
        "thumbnail_url": str(data.get("thumbnail") or ""),
        "published_at": epoch_seconds(data.get("datePublished")),
        "likes_count": as_int(likes),
        "views_count": as_int(views),
        # Treat priced videos as premium in the feed projection.
        # Firestore remains authoritative for actual purchase access.
        "is_premium": int(cost > 0),
        "cost": cost,
        "language": str(data.get("language") or "English"),
        "is_available": 1,
        "images_json": "[]",
        "comment_count": as_int(data.get("commentCount")),
        "sync_run_id": run_id,
        "likes_json": json.dumps(likes, ensure_ascii=False, default=str),
        "viewers_json": None,
    }


def normalize_post(doc, run_id):
    data = doc.to_dict() or {}

    cost = as_int(data.get("cost"))
    likes = data.get("likes") or []
    views = data.get("views") or []
    raw_images = data.get("images") or []

    # Preserve all image entries, including Cloudinary metadata.
    images = [
        image for image in raw_images
        if isinstance(image, dict)
    ]

    return {
        "content_id": f"post:{doc.id}",
        "source_id": doc.id,
        "content_type": "post",
        "creator_id": str(data.get("userId") or ""),
        "title": str(data.get("title") or ""),
        "description": str(data.get("text") or ""),
        "media_url": "",
        "thumbnail_url": first_image_url(images),
        "published_at": epoch_seconds(data.get("datePublished")),
        "likes_count": as_int(likes),
        # Post views are viewer IDs; use their count for ranking.
        "views_count": as_int(views),
        "is_premium": int(bool(data.get("isPremium", False))),
        "cost": cost,
        "language": str(data.get("language") or "English"),
        "is_available": 1,
        "images_json": json.dumps(images, ensure_ascii=False, default=str),
        "comment_count": as_int(data.get("commentCount")),
        "sync_run_id": run_id,
        "likes_json": json.dumps(
            likes, ensure_ascii=False, default=str
        ),
        "viewers_json": json.dumps(
            views, ensure_ascii=False, default=str
        ),
    }


UPSERT_SQL = """
INSERT INTO feed_content (
    content_id, source_id, content_type, creator_id,
    title, description, media_url, thumbnail_url,
    published_at, likes_count, views_count,
    is_premium, cost, language, is_available,
    synced_at, images_json, comment_count, sync_run_id, likes_json, viewers_json
)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(content_id) DO UPDATE SET
    source_id = excluded.source_id,
    content_type = excluded.content_type,
    creator_id = excluded.creator_id,
    title = excluded.title,
    description = excluded.description,
    media_url = excluded.media_url,
    thumbnail_url = excluded.thumbnail_url,
    published_at = excluded.published_at,
    likes_count = excluded.likes_count,
    views_count = excluded.views_count,
    is_premium = excluded.is_premium,
    cost = excluded.cost,
    language = excluded.language,
    is_available = excluded.is_available,
    synced_at = excluded.synced_at,
    images_json = excluded.images_json,
    comment_count = excluded.comment_count,
    sync_run_id = excluded.sync_run_id,
    likes_json = excluded.likes_json,
    viewers_json = excluded.viewers_json
"""


def main():
    service_account = json.loads(
        required_env("FIREBASE_SERVICE_ACCOUNT_JSON")
    )
    database_url = required_env("TURSO_DATABASE_URL")
    auth_token = required_env("TURSO_AUTH_TOKEN")

    # Read both collections successfully before modifying Turso.
    if not firebase_admin._apps:
        firebase_admin.initialize_app(
            credentials.Certificate(service_account)
        )

    fs = firestore.client()

    videos = list(fs.collection("videos").stream())
    posts = list(fs.collection("posts").stream())

    records = [
        normalize_video(doc, run_id="pending")
        for doc in videos
    ]
    records.extend(
        normalize_post(doc, run_id="pending")
        for doc in posts
    )

    # Prevent an unexpected empty Firestore response from wiping Turso.
    if not records and os.environ.get("ALLOW_EMPTY_SYNC") != "true":
        raise RuntimeError(
            "Both collections returned zero documents. "
            "Aborting deletion cleanup. Set ALLOW_EMPTY_SYNC=true "
            "only if you intentionally want an empty feed."
        )

    run_id = str(uuid.uuid4())
    now = int(datetime.now(timezone.utc).timestamp())

    for record in records:
        record["sync_run_id"] = run_id

    conn = turso_serverless.connect(
        database_url,
        auth_token=auth_token,
    )

    try:
        for record in records:
            values = (
                record["content_id"],
                record["source_id"],
                record["content_type"],
                record["creator_id"],
                record["title"],
                record["description"],
                record["media_url"],
                record["thumbnail_url"],
                record["published_at"],
                record["likes_count"],
                record["views_count"],
                record["is_premium"],
                record["cost"],
                record["language"],
                record["is_available"],
                now,
                record["images_json"],
                record["comment_count"],
                record["sync_run_id"],
                record["likes_json"],
                record["viewers_json"],
            )
            conn.execute(UPSERT_SQL, values)

        # Commit the upserts before deleting stale records.
        conn.commit()

        # Only records absent from this successful full sync are removed.
        conn.execute(
            "DELETE FROM feed_content WHERE sync_run_id IS NULL "
            "OR sync_run_id != ?",
            (run_id,),
        )
        conn.commit()

        print(f"Sync completed. Videos: {len(videos)}")
        print(f"Posts: {len(posts)}")
        print(f"Total records synced: {len(records)}")
        print(f"Sync run: {run_id}")

    finally:
        conn.close()


if __name__ == "__main__":
    main()
