"""
ゆりか プロジェクト 週次リサーチスクリプト（ローカル実行版）

毎週月曜、launchdから呼び出される想定。
Google AI Studio（Gemini API）のWeb検索ツールで最新トレンドを調べ、Firestoreに保存する。
"""

import json
import os

import firebase_admin
import requests
from firebase_admin import credentials, firestore

SERVICE_ACCOUNT_PATH = os.path.join(
    os.path.dirname(__file__), "serviceAccountKey.json"
)
RESEARCH_COLLECTION = "yurika_research"

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GEMINI_MODEL = "gemini-2.5-flash"

PROMPT = """
あなたは「キャリアと結婚・出産で迷う30代女性」向けコンテンツのリサーチ担当です。
以下のテーマについて、直近1週間で話題になっているニュース・トレンドをWeb検索で調べてください。

テーマ：卵子凍結、妊活、不妊、キャリアと結婚・出産の両立、ライフプラン、著名人の妊娠・結婚報道

出力は以下のJSON形式のみで返してください（説明文やコードブロック記号は不要）：
[
  {
    "title": "ネタのタイトル案",
    "summary": "何が話題になっているか、2-3文で要約",
    "source_hint": "情報源のヒント（サイト名など、URLが不明ならジャンルのみでも可）",
    "angle": "このジャンルの発信者としてどう切り口にできるか"
  }
]
最大5件まで。話題性が低い、または不確かな情報は含めないでください。
""".strip()


def main():
    if not GEMINI_API_KEY:
        print("環境変数 GEMINI_API_KEY が未設定です")
        return

    cred = credentials.Certificate(SERVICE_ACCOUNT_PATH)
    if not firebase_admin._apps:
        firebase_admin.initialize_app(cred)
    db = firestore.client()

    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    )
    res = requests.post(
        url,
        json={
            "contents": [{"parts": [{"text": PROMPT}]}],
            "tools": [{"google_search": {}}],
        },
    )
    data = res.json()
    if not res.ok:
        print("リサーチAPI呼び出し失敗:", data)
        return

    try:
        text_blocks = data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError):
        print("リサーチAPIの返答が想定外の形式でした:", data)
        return

    try:
        cleaned = text_blocks.replace("```json", "").replace("```", "").strip()
        items = json.loads(cleaned)
    except Exception as e:
        print("リサーチ結果のJSONパースに失敗:", e, text_blocks)
        return

    batch = db.batch()
    for item in items:
        ref = db.collection(RESEARCH_COLLECTION).document()
        batch.set(
            ref,
            {
                **item,
                "createdAt": firestore.SERVER_TIMESTAMP,
                "status": "new",
            },
        )
    batch.commit()
    print(f"リサーチ結果を{len(items)}件保存しました")


if __name__ == "__main__":
    main()
