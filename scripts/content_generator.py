"""
ゆりか プロジェクト AIチーム自動生成スクリプト（ローカル実行版）

やること：
1. yurika_research（週次リサーチ結果）から未使用のネタを1つ取得
   （ネタが無ければ、あらかじめ用意した定番テーマからランダムに選ぶ）
2. 「ライター」役でThreads投稿文の下書きを生成（ゆりかペルソナ・トーン厳守）
3. 「チェック担当」役でNGライン（誘導表現・障害児関連の断定など）に違反してないか確認し、
   問題があれば自動で修正させる
4. 完成した投稿文を yurika_posts に status='queued' で書き込む
   （その後は threads_worker.py が重複チェック・12時間キャンセル猶予・投稿を担当する）

launchdから毎日1回呼び出される想定。
"""

import json
import os
import random
from datetime import datetime

import firebase_admin
import requests
from firebase_admin import credentials, firestore

SERVICE_ACCOUNT_PATH = os.path.join(
    os.path.dirname(__file__), "serviceAccountKey.json"
)
POSTS_COLLECTION = "yurika_posts"
RESEARCH_COLLECTION = "yurika_research"

# Google AI Studio（Gemini API）を使用。クレジットカード不要の無料枠で利用可能。
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GEMINI_MODEL = "gemini-2.5-flash"

# ==== ペルソナ・ルール（persona_yurika.md の要約版） ====
PERSONA = """
あなたは「ゆりか」というペルソナで発信するThreads投稿のライターです。

【プロフィール】
35歳、看護師（整形外科病棟勤務）。33歳で彼氏と別れて以来2年近く彼氏なし。
結婚・子育てへの興味はまだあるが出会いがない。仕事は順調だが一生このままとは思っていない。
休日はダラダラ過ごすことが多い。旅行は好き。

【心情の核】
芸能人の結婚・妊娠報道を見ると素直に喜べない自分がいる。友人との会話で疎外感を覚える。
生理が来ると少し安心する（まだ妊娠できるという確認）。高齢出産のリスクは知識として知っているが、
障害のある子を育てる覚悟が今あるかと言われたら正直「ない」と思っている。
「一人で生きるのもいい」という気持ちと「本当にそれが望む未来か」という迷いが同居している。

【トーン：しがない看護師】
- キラキラした「頑張ってます」ではなく、等身大で少し冴えない日常を送っている人という自己認識
- 断定を避け、「〜なんだろう」「〜なのかな」と自問する言い回しが多い
- 一文が短め。正直な弱さを隠さず書く（これが信頼を生む）
- 啓発的な結論で締めなくていい。「まだ答えは出てない」で終わってもいい

【絶対厳守のNGライン】
- 「産む/産まない」「結婚すべき/しなくていい」など、読者の人生選択を誘導する表現は絶対禁止
- 障害児・出生前診断の話題は、どちらかを勧める書き方を絶対にしない
- 医学的情報を断定的に書かない（不確かな数字は使わない）
- 実在の人物の心情を事実確認なしに断定しない
""".strip()

FALLBACK_TOPICS = [
    "友達の結婚式に呼ばれた時の複雑な気持ち",
    "婚活アプリを1年やって疲れた話",
    "同僚の妊娠報告を聞いた日の気持ち",
    "『まだ結婚しないの』と言われた時にどう答えるか",
    "一人の時間が好きな自分と、将来への不安が同居してる話",
    "看護師として見てきた、高齢出産の患者さんのこと（プライバシーに配慮した一般論として）",
]


def call_gemini(prompt: str) -> str:
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    )
    res = requests.post(
        url,
        json={"contents": [{"parts": [{"text": prompt}]}]},
    )
    data = res.json()
    if not res.ok:
        raise RuntimeError(f"Gemini API呼び出し失敗: {data}")
    try:
        return data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError):
        raise RuntimeError(f"Gemini APIの返答が想定外の形式でした: {data}")


def pick_topic(db):
    docs = list(
        db.collection(RESEARCH_COLLECTION)
        .where("status", "==", "new")
        .limit(1)
        .stream()
    )
    if docs:
        doc = docs[0]
        data = doc.to_dict()
        doc.reference.update({"status": "used"})
        return f"{data.get('title', '')}：{data.get('summary', '')}（切り口案：{data.get('angle', '')}）"
    return random.choice(FALLBACK_TOPICS)


def write_draft(topic: str) -> str:
    prompt = f"""{PERSONA}

【今日のお題】
{topic}

上記のペルソナ・トーン・NGラインを厳守して、Threads用の投稿文を1本書いてください。

出力ルール：
- **必ず200字以内**（絶対にこれを超えるな。短く書け。）
- 冒頭で引き込み、最後は啓発的にまとめすぎない
- 説明文やタイトルは不要、投稿本文のみを出力
"""
    return call_gemini(prompt).strip()


def check_and_fix(draft: str) -> str:
    prompt = f"""{PERSONA}

あなたは校閲・品質チェック担当です。以下の投稿文をレビューしてください。

【投稿文】
{draft}

チェック観点：
1. 「産む/産まない」等、読者の人生選択を誘導する表現がないか
2. 障害児・出生前診断について、どちらかを勧める書き方になっていないか
3. 断定的すぎる医学情報がないか
4. ペルソナ（しがない看護師トーン）から外れていないか

問題がなければ、投稿文をそのまま一字一句変えずに出力してください。
問題があれば、該当箇所を修正した投稿文全文を出力してください。
説明文やコメントは一切不要、最終的な投稿文のみを出力してください。
"""
    return call_gemini(prompt).strip()


def main():
    if not GEMINI_API_KEY or GEMINI_API_KEY == "dummy":
        print("GEMINI_API_KEY が未設定（またはdummyのまま）です。生成をスキップします。")
        return

    cred = credentials.Certificate(SERVICE_ACCOUNT_PATH)
    if not firebase_admin._apps:
        firebase_admin.initialize_app(cred)
    db = firestore.client()

    topic = pick_topic(db)
    print(f"今日のお題: {topic}")

    draft = write_draft(topic)
    print("下書き生成完了")

    final_text = check_and_fix(draft)
    print("チェック完了")

    db.collection(POSTS_COLLECTION).add(
        {
            "text": final_text,
            "createdAt": firestore.SERVER_TIMESTAMP,
            "status": "queued",
            "source": "auto_content_generator",
            "topic": topic,
        }
    )
    print("投稿をキューに追加しました（この後、通常通り12時間のキャンセル猶予を経て投稿されます）")


if __name__ == "__main__":
    main()
