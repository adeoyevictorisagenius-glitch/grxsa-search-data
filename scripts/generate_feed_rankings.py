import hashlib
import json
import os
from datetime import datetime, timezone

import firebase_admin
from firebase_admin import credentials, firestore
import turso_serverless


# --------------------------------------------------
# CONFIGURATION
# --------------------------------------------------

def required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


# --------------------------------------------------
# HELPERS
# --------------------------------------------------

def as_int(value, default=0):
    try:
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, (list, tuple, set, dict)):
            return len(value)
        return int(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def parse_json_list(value):
    """Parse a JSON list safely."""
    if isinstance(value, list):
        return value

    if not value:
        return []

    try:
        result = json.loads(value)
        return result if isinstance(result, list) else []
    except (TypeError, ValueError, json.JSONDecodeError):
        return []


def stable_tiebreaker(user_id, content_id):
    """Small deterministic bonus; never randomly reshuffles feeds."""
    key = f"{user_id}:{content_id}".encode("utf-8")
    digest = hashlib.sha256(key).hexdigest()
    return (int(digest[:8], 16) / 0xFFFFFFFF) * 0.0001


def freshness_score(published_at, now):
    if not published_at:
        return 0.0

    hours_old = max(0, (now - published_at) / 3600)
    return max(0.0, 30.0 - hours_old / 4.0)


def result_to_dicts(result):
    """
    Support common DB-API cursor results and list-based results.
    Raises an error rather than silently ranking an empty catalogue.
    """
    if hasattr(result, "fetchall"):
        rows = result.fetchall()
        description = getattr(result, "description", None)
        columns = [item[0] for item in description] if description else []
    elif isinstance(result, list):
        rows = result
        columns = []
    elif hasattr(result, "rows"):
        rows = result.rows
        columns = list(getattr(result, "columns", []) or [])
    else:
        raise RuntimeError(
            "Unsupported Turso query result. "
            f"Received: {type(result).__name__}"
        )

    output = []

    for row in rows:
        if isinstance(row, dict):
            output.append(row)
        elif hasattr(row, "keys"):
            output.append({key: row[key] for key in row.keys()})
        elif columns:
            output.append(dict(zip(columns, row)))
        else:
            raise RuntimeError(
                "Turso returned rows without column names. "
                "Check result_to_dicts() against the installed "
                "turso_serverless result format."
            )

    return output


# --------------------------------------------------
# READ CONTENT FROM TURSO
# --------------------------------------------------

def load_content(conn):
    result = conn.execute("""
        SELECT
            content_id,
            source_id,
            content_type,
            creator_id,
            title,
            description,
            published_at,
            likes_count,
            views_count,
            is_premium,
            cost,
            language,
            is_available,
            likes_json,
            viewers_json
        FROM feed_content
        WHERE is_available = 1
    """)

    records = result_to_dicts(result)

    if not records:
        raise RuntimeError(
            "No available content found in feed_content. "
            "Existing feeds will not be replaced."
        )

    # Normalize JSON fields and numbers once.
    for item in records:
        item["content_id"] = str(item.get("content_id") or "")
        item["source_id"] = str(item.get("source_id") or "")
        item["content_type"] = str(item.get("content_type") or "")
        item["creator_id"] = str(item.get("creator_id") or "")
        item["likes_count"] = as_int(item.get("likes_count"))
        item["views_count"] = as_int(item.get("views_count"))
        item["published_at"] = as_int(item.get("published_at"))
        item["is_premium"] = as_int(item.get("is_premium"))
        item["cost"] = as_int(item.get("cost"))

        item["likes"] = {
            str(uid)
            for uid in parse_json_list(item.get("likes_json"))
            if uid is not None
        }

        # NULL means individual viewer IDs are unavailable.
        item["viewers"] = (
            None
            if item.get("viewers_json") is None
            else {
                str(uid)
                for uid in parse_json_list(item.get("viewers_json"))
                if uid is not None
            }
        )

    return records


# --------------------------------------------------
# USER-SPECIFIC FIRESTORE DATA
# --------------------------------------------------

def load_users(fs):
    """
    Load user documents and their purchased-video IDs.
    Content documents are NOT read from Firestore here.
    """
    user_docs = list(fs.collection("users").stream())

    if not user_docs:
        raise RuntimeError(
            "No user documents found. Existing feeds will not be replaced."
        )

    users = []

    for doc in user_docs:
        data = doc.to_dict() or {}
        user_id = doc.id

        subscriptions = data.get("subscriptions") or []

        if not isinstance(subscriptions, (list, tuple, set)):
            subscriptions = []

        purchased_video_ids = {
            str(purchase_doc.id)
            for purchase_doc in (
                fs.collection("users")
                .document(user_id)
                .collection("purchases")
                .stream()
            )
        }

        users.append({
            "user_id": user_id,
            "subscriptions": {
                str(uid) for uid in subscriptions if uid is not None
            },
            "purchased_video_ids": purchased_video_ids,
        })

    return users


# --------------------------------------------------
# RANKING
# --------------------------------------------------

def calculate_default_score(item, now):
    """Shared discovery ranking; no user-specific signals."""
    score = 0.0

    score += item["likes_count"] * 2
    score += item["views_count"] / 100
    score += freshness_score(item["published_at"], now)

    score += stable_tiebreaker(
        "default",
        item["content_id"],
    )

    return score


def calculate_personalized_score(item, user, now):
    """Preserves the core scoring signals from FeedService."""
    score = 0.0
    user_id = user["user_id"]

    # Followed creators.
    if item["creator_id"] in user["subscriptions"]:
        score += 50

    # Engagement.
    score += item["likes_count"] * 2
    score += item["views_count"] / 100

    # Freshness.
    score += freshness_score(item["published_at"], now)

    # The creator's own content.
    if item["creator_id"] == user_id:
        score += 20

    if item["content_type"] == "video":
        # The current video schema only provides aggregate view counts.
        # Do not invent per-user video viewing history.
        if item["source_id"] not in user["purchased_video_ids"]:
            score += 25

    elif item["content_type"] == "post":
        viewer_ids = item["viewers"] or set()
        has_seen = user_id in viewer_ids

        # Promote posts this user has not yet seen or unlocked.
        if not has_seen:
            score += 25

        # In the existing GRXSA design, premium post views contain
        # buyer UIDs after successful purchases.
        if item["is_premium"] and not has_seen:
            score += 30

    # Stable tie-breaking, instead of Random().nextDouble() * 10.
    score += stable_tiebreaker(user_id, item["content_id"])

    return score


def rank_default_feed(content, now):
    ranked = []

    for item in content:
        score = calculate_default_score(item, now)
        ranked.append((item["content_id"], score))

    ranked.sort(key=lambda pair: (-pair[1], pair[0]))

    return [
        {
            "content_id": content_id,
            "score": score,
            "position": position,
        }
        for position, (content_id, score) in enumerate(ranked)
    ]


def rank_user_feed(content, user, now):
    ranked = []

    for item in content:
        score = calculate_personalized_score(item, user, now)
        ranked.append((item["content_id"], score))

    ranked.sort(key=lambda pair: (-pair[1], pair[0]))

    return [
        {
            "content_id": content_id,
            "score": score,
            "position": position,
        }
        for position, (content_id, score) in enumerate(ranked)
    ]


# --------------------------------------------------
# WRITE RANKINGS TO TURSO
# --------------------------------------------------

def save_rankings(conn, default_ranking, user_rankings, now):
    """
    Replace the rankings only after all source data and rankings
    have been successfully prepared.
    """
    try:
        conn.execute("BEGIN")

        conn.execute("DELETE FROM default_feed")

        for item in default_ranking:
            conn.execute("""
                INSERT INTO default_feed (
                    content_id, score, position, generated_at
                )
                VALUES (?, ?, ?, ?)
            """, (
                item["content_id"],
                item["score"],
                item["position"],
                now,
            ))

        # Replace personalized feed rows in one transaction.
        conn.execute("DELETE FROM user_feed")

        for user_id, ranking in user_rankings.items():
            for item in ranking:
                conn.execute("""
                    INSERT INTO user_feed (
                        user_id, content_id, score,
                        position, generated_at
                    )
                    VALUES (?, ?, ?, ?, ?)
                """, (
                    user_id,
                    item["content_id"],
                    item["score"],
                    item["position"],
                    now,
                ))

            conn.execute("""
                INSERT INTO user_feed_state (
                    user_id, generated_at, ranking_version
                )
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    generated_at = excluded.generated_at,
                    ranking_version = excluded.ranking_version
            """, (
                user_id,
                now,
                "v1",
            ))

        conn.commit()

    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise


# --------------------------------------------------
# MAIN
# --------------------------------------------------

def main():
    service_account = json.loads(
        required_env("FIREBASE_SERVICE_ACCOUNT_JSON")
    )
    database_url = required_env("TURSO_DATABASE_URL")
    auth_token = required_env("TURSO_AUTH_TOKEN")

    if not firebase_admin._apps:
        firebase_admin.initialize_app(
            credentials.Certificate(service_account)
        )

    fs = firestore.client()

    conn = turso_serverless.connect(
        database_url,
        auth_token=auth_token,
    )

    try:
        now = int(datetime.now(timezone.utc).timestamp())

        print("Loading content from Turso...")
        content = load_content(conn)
        print(f"Available content: {len(content)}")

        print("Loading user preferences and purchases from Firestore...")
        users = load_users(fs)
        print(f"Users found: {len(users)}")

        print("Generating default discovery feed...")
        default_ranking = rank_default_feed(content, now)

        print("Generating personalized feeds...")
        user_rankings = {}

        for index, user in enumerate(users, start=1):
            user_rankings[user["user_id"]] = rank_user_feed(
                content,
                user,
                now,
            )

            if index % 25 == 0:
                print(f"Ranked {index}/{len(users)} users...")

        print("Saving all rankings to Turso...")
        save_rankings(
            conn,
            default_ranking,
            user_rankings,
            now,
        )

        print("Feed ranking generation completed successfully.")
        print(f"Default feed entries: {len(default_ranking)}")
        print(f"Personalized feeds generated: {len(user_rankings)}")
        print(f"Total personalized entries: {sum(map(len, user_rankings.values()))}")
        print(f"Generated at: {now}")

    finally:
        conn.close()


if __name__ == "__main__":
    main()
