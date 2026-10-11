"""
GitHub Actions Cloud Subtitle Worker
------------------------------------
Executes inside GitHub Actions runners:
1. Receives job payload (anime_id, ep_num, doc_id, category, episode_path).
2. Fetches MegaPlay English subtitle directly.
3. Translates to Sinhala using megaplay_sub_engine with Spoken Sinhala dictionary.
4. Uploads .vtt to GitHub Releases to get a permanent DDL link.
5. Saves the subtitle link in Firestore: stream_subtitles.sinhala = "<github_vtt_url>".
6. Emits completion signal to RTDB ('megaplay_sub_jobs/completed/{job_id}') for Manager bot.
"""

import os
import sys
import json
import time
import requests
import firebase_admin
from firebase_admin import credentials, firestore, db as rtdb
from dotenv import load_dotenv

from megaplay_sub_engine import (
    fetch_megaplay_english_sub,
    process_and_translate_vtt,
    upload_sub_to_github_release
)

load_dotenv()

FIREBASE_DB_URL = os.getenv("FIREBASE_DB_URL", "https://anishift-5d14b-default-rtdb.firebaseio.com/")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
GITHUB_REPO = os.getenv("GITHUB_REPO", "").strip()

def init_firebase():
    key_path = "serviceAccountKey.json"
    if not os.path.exists(key_path):
        fb_json = os.getenv("FIREBASE_JSON")
        if fb_json:
            with open(key_path, "w", encoding="utf-8") as f:
                f.write(fb_json)
        else:
            raise RuntimeError("Missing serviceAccountKey.json / FIREBASE_JSON")

    if not firebase_admin._apps:
        cred = credentials.Certificate(key_path)
        firebase_admin.initialize_app(cred, {"databaseURL": FIREBASE_DB_URL})
    return firestore.client()

def main():
    payload_str = os.getenv("JOB_PAYLOAD")
    if not payload_str:
        print("❌ Missing JOB_PAYLOAD environment variable!", flush=True)
        sys.exit(1)

    payload = json.loads(payload_str)
    job_id = payload.get("job_id")
    anime_id = str(payload.get("anime_id"))
    ep_num = int(payload.get("ep_num", 1))
    episode_path = payload.get("episode_path")

    print(f"🎬 [CloudSubWorker] Starting Job {job_id}: Anime {anime_id} EP {ep_num} ({episode_path})", flush=True)

    db = init_firebase()

    # 1. Fetch English Subtitle from MegaPlay
    print(f"📥 Fetching English Subtitle from MegaPlay...", flush=True)
    eng_vtt, err = fetch_megaplay_english_sub(anime_id, ep_num)
    if err or not eng_vtt:
        print(f"⚠️ Failed fetching English sub: {err}", flush=True)
        # Notify RTDB of failure
        rtdb.reference(f"megaplay_sub_jobs/completed/{job_id}").set({
            "status": "failed",
            "error": err or "Subtitle not found",
            "anime_id": anime_id,
            "ep_num": ep_num,
            "timestamp": time.time()
        })
        sys.exit(0)

    # 2. Translate to Sinhala
    print(f"🌐 Translating English WebVTT to Spoken Sinhala...", flush=True)
    sinhala_vtt = process_and_translate_vtt(eng_vtt, max_threads=15)
    if not sinhala_vtt:
        print(f"⚠️ Translation returned empty content", flush=True)
        rtdb.reference(f"megaplay_sub_jobs/completed/{job_id}").set({
            "status": "failed",
            "error": "Translation failed",
            "anime_id": anime_id,
            "ep_num": ep_num,
            "timestamp": time.time()
        })
        sys.exit(1)

    # 3. Upload to GitHub Releases
    print(f"☁️ Uploading Sinhala .vtt to GitHub Releases ({GITHUB_REPO})...", flush=True)
    sub_url = upload_sub_to_github_release(sinhala_vtt, anime_id, ep_num, GITHUB_TOKEN, GITHUB_REPO)
    print(f"✅ Subtitle uploaded successfully: {sub_url}", flush=True)

    # 4. Save in Firestore episode document: stream_subtitles.sinhala (and subtitles.sinhala)
    print(f"💾 Updating Firestore document: {episode_path}...", flush=True)
    ep_ref = db.document(episode_path)
    ep_ref.set({
        "stream_subtitles": {
            "sinhala": sub_url
        },
        "subtitles": {
            "sinhala": sub_url
        }
    }, merge=True)

    # 5. Emit completed signal to RTDB for Manager and save in RTDB sub status
    rtdb.reference(f"megaplay_sub_jobs/completed/{job_id}").set({
        "status": "success",
        "url": sub_url,
        "anime_id": anime_id,
        "ep_num": ep_num,
        "timestamp": time.time()
    })
    # Cache under anime status so manager knows it's done without Firestore reads
    try:
        rtdb.reference(f"megaplay_sub_status/{anime_id}/episodes/{ep_num}").set({
            "url": sub_url,
            "updated_at": time.time()
        })
    except Exception:
        pass

    print(f"🎉 Job {job_id} finished 100% successfully!", flush=True)

if __name__ == "__main__":
    main()
