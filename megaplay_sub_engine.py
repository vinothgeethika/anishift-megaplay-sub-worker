"""
Clean MegaPlay Universal Subtitle Engine
----------------------------------------
Fetches English subtitles from MegaPlay (Server 3),
Translates into Spoken Sinhala using high-speed multi-threaded translation,
Uploads Sinhala WebVTT to GitHub Releases (DDL),
Saves direct link in Firestore under 'stream_subtitles.sinhala',
and signals job completion to RTDB.
(Contains NO ad cleaners, spam filters, or useless regexes).
"""

import os
import re
import sys
import time
import json
import base64
import requests
import chardet
import concurrent.futures
from deep_translator import GoogleTranslator

# Spoken Sinhala dictionary
try:
    from spoken_dict import SPOKEN_DICT
except ImportError:
    SPOKEN_DICT = {}

def apply_spoken_sinhala(text):
    if not text or not SPOKEN_DICT: return text
    sorted_keys = sorted(SPOKEN_DICT.keys(), key=len, reverse=True)
    result_text = str(text)
    for key in sorted_keys:
        value = SPOKEN_DICT[key]
        pattern = r'(?<![\w\u0D80-\u0DFF])' + re.escape(key) + r'(?![\w\u0D80-\u0DFF])'
        result_text = re.sub(pattern, value, result_text)
    return result_text

def has_sinhala_characters(text):
    return bool(re.search(r'[\u0D80-\u0DFF]', str(text)))

def fetch_megaplay_english_sub(anime_id, ep_num):
    """Fetches English .vtt subtitle URL directly from MegaPlay stream metadata"""
    embed_url = f"https://megaplay.buzz/stream/ani/{anime_id}/{ep_num}/sub"
    headers = {
        "Referer": "https://anishift.netlify.app/",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    }

    resp = requests.get(embed_url, headers=headers, timeout=15)
    if resp.status_code in (404, 410):
        return None, "Episode not found on MegaPlay"

    match = re.search(r'data-id=["\'](\d+)["\']', resp.text)
    if not match:
        return None, "MegaPlay data-id not found"

    data_id = match.group(1)
    sources_url = f"https://megaplay.buzz/stream/getSources?id={data_id}"
    sources_resp = requests.get(sources_url, headers={"Referer": "https://megaplay.buzz/", "User-Agent": headers["User-Agent"]}, timeout=15)
    if sources_resp.status_code != 200:
        return None, f"getSources failed with {sources_resp.status_code}"

    data = sources_resp.json()
    tracks = data.get("tracks", [])
    eng_track_url = None
    for t in tracks:
        file_url = t.get("file", "")
        label = (t.get("label") or "").lower()
        if "english" in label or "eng" in file_url:
            eng_track_url = file_url
            break

    if not eng_track_url and tracks:
        # Fallback to first available subtitle track
        eng_track_url = tracks[0].get("file")

    if not eng_track_url:
        return None, "No subtitle track available on MegaPlay"

    # Fetch the actual raw VTT content
    sub_resp = requests.get(eng_track_url, headers={"Referer": "https://megaplay.buzz/", "User-Agent": headers["User-Agent"]}, timeout=20)
    if sub_resp.status_code != 200:
        return None, f"Failed downloading VTT file: {sub_resp.status_code}"

    return sub_resp.text, None

def parse_vtt_cues(vtt_text):
    """Extracts (timecode, text) cues from WebVTT"""
    lines = vtt_text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    cues = []
    current_time = None
    current_lines = []

    for line in lines:
        if "-->" in line:
            if current_time and current_lines:
                cues.append({"time": current_time, "text": " ".join(current_lines)})
            current_time = line
            current_lines = []
        elif current_time:
            if line.strip() == "":
                cues.append({"time": current_time, "text": " ".join(current_lines)})
                current_time = None
                current_lines = []
            else:
                current_lines.append(line.strip())

    if current_time and current_lines:
        cues.append({"time": current_time, "text": " ".join(current_lines)})

    return cues

def translate_cue_batch(batch_texts):
    """Translates a batch of texts separated by ' ||| ' using GoogleTranslator with retry"""
    if not batch_texts:
        return []
    combined = " ||| ".join(batch_texts)

    for attempt in range(3):
        try:
            translator = GoogleTranslator(source="en", target="si")
            translated = translator.translate(combined)
            parts = [p.strip() for p in translated.split("|||")]
            if len(parts) == len(batch_texts):
                return [apply_spoken_sinhala(p) for p in parts]
            # If parts count mismatch, split and pad
            padded = []
            for i in range(len(batch_texts)):
                val = parts[i] if i < len(parts) else batch_texts[i]
                padded.append(apply_spoken_sinhala(val))
            return padded
        except Exception:
            time.sleep(1.5)

    # Fallback to original texts on error
    return batch_texts

def process_and_translate_vtt(vtt_content, max_threads=15):
    """Multi-threaded VTT translation pipeline (Fast & Complete)"""
    cues = parse_vtt_cues(vtt_content)
    if not cues:
        return None

    BATCH_SIZE = 30
    batches = []
    for i in range(0, len(cues), BATCH_SIZE):
        chunk = cues[i:i + BATCH_SIZE]
        batches.append([c["text"] for c in chunk])

    translated_results = [None] * len(batches)

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_threads) as executor:
        future_to_idx = {executor.submit(translate_cue_batch, batch): idx for idx, batch in enumerate(batches)}
        for future in concurrent.futures.as_completed(future_to_idx):
            idx = future_to_idx[future]
            try:
                translated_results[idx] = future.result()
            except Exception:
                translated_results[idx] = batches[idx]

    # Reassemble WebVTT
    out_lines = ["WEBVTT", "Kind: captions", "Language: si", ""]
    cue_idx = 1
    for b_idx, batch_trans in enumerate(translated_results):
        batch_cues = cues[b_idx * BATCH_SIZE : (b_idx + 1) * BATCH_SIZE]
        for c_idx, cue in enumerate(batch_cues):
            si_text = batch_trans[c_idx] if batch_trans and c_idx < len(batch_trans) else cue["text"]
            out_lines.append(str(cue_idx))
            out_lines.append(cue["time"])
            out_lines.append(si_text)
            out_lines.append("")
            cue_idx += 1

    return "\n".join(out_lines)

def upload_sub_to_github_release(vtt_text, anime_id, ep_num, github_token, github_repo):
    """Uploads Sinhala WebVTT as direct asset to a GitHub Release, returning permanent DDL"""
    headers = {
        "Authorization": f"Bearer {github_token}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "MegaPlaySubBot/1.0"
    }

    tag_name = f"sub-ani-{anime_id}"
    rel_url = f"https://api.github.com/repos/{github_repo}/releases/tags/{tag_name}"
    resp = requests.get(rel_url, headers=headers, timeout=15)

    release_id = None
    upload_url_template = None

    if resp.status_code == 200:
        rel_data = resp.json()
        release_id = rel_data["id"]
        upload_url_template = rel_data["upload_url"]
    else:
        # Create Release
        create_url = f"https://api.github.com/repos/{github_repo}/releases"
        c_resp = requests.post(create_url, headers=headers, json={
            "tag_name": tag_name,
            "name": f"Subtitles - Anime {anime_id}",
            "body": f"Automated WebVTT subtitles for Anime ID {anime_id}",
            "draft": False,
            "prerelease": False
        }, timeout=15)
        if c_resp.status_code in (200, 201):
            rel_data = c_resp.json()
            release_id = rel_data["id"]
            upload_url_template = rel_data["upload_url"]
        else:
            raise RuntimeError(f"Failed creating GitHub release: {c_resp.status_code} {c_resp.text}")

    # Asset upload
    base_upload_url = upload_url_template.split("{")[0]
    asset_name = f"ep_{ep_num}_sinhala.vtt"

    # Delete existing asset if present
    assets_url = f"https://api.github.com/repos/{github_repo}/releases/{release_id}/assets"
    a_resp = requests.get(assets_url, headers=headers, timeout=15)
    if a_resp.status_code == 200:
        for asset in a_resp.json():
            if asset.get("name") == asset_name:
                del_url = f"https://api.github.com/repos/{github_repo}/releases/assets/{asset['id']}"
                requests.delete(del_url, headers=headers, timeout=15)
                break

    upload_headers = {
        "Authorization": f"Bearer {github_token}",
        "Content-Type": "text/vtt; charset=utf-8",
        "User-Agent": "MegaPlaySubBot/1.0"
    }

    up_resp = requests.post(
        f"{base_upload_url}?name={asset_name}",
        headers=upload_headers,
        data=vtt_text.encode("utf-8"),
        timeout=30
    )

    if up_resp.status_code in (200, 201):
        asset_data = up_resp.json()
        return asset_data.get("browser_download_url")
    else:
        raise RuntimeError(f"Failed uploading asset: {up_resp.status_code} {up_resp.text}")
