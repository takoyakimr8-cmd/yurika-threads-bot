"""
ゆりか Threads 半自動投稿ワーカー（ローカル実行版）

このスクリプトは launchd から15分ごとに呼び出されることを想定しています。

やること：
1. status='queued' の投稿を確認し、重複チェック → 'pending'（12時間後に投稿予定）
   または 'duplicate'（重複のため投稿しない）に振り分ける
2. 「今日まだ3件投稿してない」かつ「前回投稿から3時間以上経ってる」場合、
   scheduledAt を過ぎた pending 投稿のうち一番古いものを1件だけ投稿する
   （Macが特定の時刻に開いてなくても、次に開いた時に自然に追いつく設計）
"""

import os
import re
from datetime import datetime, timedelta, timezone

import firebase_admin
import requests
from firebase_admin import credentials, firestore

# ==== 設定 ====
SERVICE_ACCOUNT_PATH = os.path.join(
    os.path.dirname(__file__), "serviceAccountKey.json"
)
POSTS_COLLECTION = "yurika_posts"
HOLD_HOURS = 12  # キャンセル猶予時間
MAX_POSTS_PER_DAY = 3  # 1日の投稿上限
MIN_GAP_HOURS = 3  # 投稿と投稿の最低間隔（連投を防ぐ）
THREADS_MAX_LENGTH = 500  # Threadsのテキスト投稿の文字数上限

JST = timezone(timedelta(hours=9))

THREADS_ACCESS_TOKEN = os.environ.get("THREADS_ACCESS_TOKEN")
THREADS_USER_ID = os.environ.get("THREADS_USER_ID")


def normalize(text: str) -> str:
    return re.sub(r"\s+", "", text or "").strip()


def can_post_now(db, now_utc: datetime) -> bool:
    """
    今日すでに何回投稿したか、最後の投稿からどれくらい経ったかを見て、
    「今投稿していいか」を判定する。
    Macが特定の時刻に開いてなくても、開いた時に自然に追いつける設計。
    """
    now_jst = now_utc.astimezone(JST)
    today_start_jst = now_jst.replace(hour=0, minute=0, second=0, microsecond=0)
    today_start_utc = today_start_jst.astimezone(timezone.utc)

    posted_today = list(
        db.collection(POSTS_COLLECTION)
        .where("status", "==", "posted")
        .where("postedAt", ">=", today_start_utc)
        .order_by("postedAt", direction=firestore.Query.DESCENDING)
        .stream()
    )

    if len(posted_today) >= MAX_POSTS_PER_DAY:
        print(f"本日は既に{len(posted_today)}件投稿済み（上限{MAX_POSTS_PER_DAY}件）")
        return False

    if posted_today:
        last_posted_at = posted_today[0].to_dict()["postedAt"]
        elapsed_hours = (now_utc - last_posted_at).total_seconds() / 3600
        if elapsed_hours < MIN_GAP_HOURS:
            print(f"前回投稿から{elapsed_hours:.1f}時間しか経ってません（最低{MIN_GAP_HOURS}時間空ける）")
            return False

    return True


def post_to_threads(text: str) -> str:
    base = "https://graph.threads.net/v1.0"

    # ステップ1：投稿コンテナを作成
    create_res = requests.get(
        f"{base}/{THREADS_USER_ID}/threads",
        params={
            "media_type": "TEXT",
            "text": text,
            "access_token": THREADS_ACCESS_TOKEN,
        },
    )
    create_data = create_res.json()
    if not create_res.ok:
        raise RuntimeError(f"作成失敗: {create_data}")
    creation_id = create_data["id"]

    # ステップ2：公開
    publish_res = requests.post(
        f"{base}/{THREADS_USER_ID}/threads_publish",
        params={
            "creation_id": creation_id,
            "access_token": THREADS_ACCESS_TOKEN,
        },
    )
    publish_data = publish_res.json()
    if not publish_res.ok:
        raise RuntimeError(f"公開失敗: {publish_data}")

    return publish_data["id"]


def main():
    if not THREADS_ACCESS_TOKEN or not THREADS_USER_ID:
        print("環境変数 THREADS_ACCESS_TOKEN / THREADS_USER_ID が未設定です")
        return

    cred = credentials.Certificate(SERVICE_ACCOUNT_PATH)
    if not firebase_admin._apps:
        firebase_admin.initialize_app(cred)
    db = firestore.client()

    now_utc = datetime.now(timezone.utc)
    now_jst = now_utc.astimezone(JST)

    # ---- ① queued の投稿を振り分ける ----
    queued_docs = list(
        db.collection(POSTS_COLLECTION).where("status", "==", "queued").stream()
    )

    if queued_docs:
        existing_docs = list(
            db.collection(POSTS_COLLECTION)
            .where("status", "in", ["pending", "posted"])
            .stream()
        )
        existing_texts = {normalize(d.to_dict().get("text", "")) for d in existing_docs}

        for doc in queued_docs:
            data = doc.to_dict()
            norm = normalize(data.get("text", ""))
            if norm in existing_texts:
                doc.reference.update(
                    {
                        "status": "duplicate",
                        "note": "同じ内容の投稿が既にあるため、自動投稿の対象外にしました",
                    }
                )
                print(f"重複投稿を検知、スキップ: {doc.id}")
            else:
                scheduled_at = now_utc + timedelta(hours=HOLD_HOURS)
                doc.reference.update(
                    {"status": "pending", "scheduledAt": scheduled_at}
                )
                existing_texts.add(norm)
                print(f"投稿を予約しました（{HOLD_HOURS}時間後）: {doc.id}")

    # ---- ② 投稿枠のタイミングなら、1件だけ投稿する ----
    if not can_post_now(db, now_utc):
        print(f"今回は投稿を見送ります（現在 {now_jst.strftime('%H:%M')} JST）")
        return

    due_docs = (
        db.collection(POSTS_COLLECTION)
        .where("status", "==", "pending")
        .where("scheduledAt", "<=", now_utc)
        .order_by("scheduledAt")
        .limit(1)
        .stream()
    )
    due_docs = list(due_docs)

    if not due_docs:
        print("この枠に投稿できる予定はなし")
        return

    doc = due_docs[0]
    data = doc.to_dict()
    text = data["text"]

    # Threadsの文字数上限を超えていたら、自然な位置で切り詰める
    if len(text) > THREADS_MAX_LENGTH:
        original_length = len(text)
        truncated = text[: THREADS_MAX_LENGTH - 1]
        # 文の区切り（。）で切れる位置を探し、なるべく不自然にならないようにする
        last_period = truncated.rfind("。")
        if last_period > THREADS_MAX_LENGTH * 0.5:  # 短くなりすぎない範囲でのみ採用
            truncated = truncated[: last_period + 1]
        text = truncated
        print(f"文字数超過のため切り詰めました: {original_length}字 -> {len(text)}字")

    try:
        threads_id = post_to_threads(text)
        doc.reference.update(
            {
                "status": "posted",
                "postedAt": now_utc,
                "threadsPostId": threads_id,
                "postedText": text,  # 実際に投稿した文章（切り詰め後）を記録
            }
        )
        print(f"投稿成功: {doc.id} -> {threads_id}")
    except Exception as e:
        doc.reference.update({"status": "failed", "error": str(e)})
        print(f"投稿失敗: {doc.id} -> {e}")


if __name__ == "__main__":
    main()
