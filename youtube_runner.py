# youtube_runner.py — 5 créneaux/jour (FR) + upload YouTube Shorts via youtube-upload
import base64, io, json, os, random, subprocess, sys, tempfile
from pathlib import Path
from typing import List
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

PARIS_TZ = ZoneInfo("Europe/Paris")
SLOTS_HOURS  = [8, 11, 14, 17, 20]
MINUTES_GRID = list(range(0, 60, 5))
GRACE_MINUTES = 10

FOLDER_IDS   = [s.strip() for s in os.environ["GDRIVE_FOLDER_IDS"].split(",") if s.strip()]
SA_JSON_B64  = os.environ["GDRIVE_SA_JSON_B64"]

CLIENT_SECRETS = Path("client_secrets.json")
CREDENTIALS    = Path("youtube_credentials.json")

DESCRIPTIONS = [
    "Compilation du jour #Shorts",
    "Moment fort — abonne-toi ! #Shorts",
    "Clip rapide ⚡ #Shorts",
]

USED_FILE     = Path("state/yt_used.json")
SCHEDULE_FILE = Path("state/yt_schedule.json")

def _load_json(path: Path, default):
    if path.exists():
        try: return json.loads(path.read_text(encoding="utf-8"))
        except Exception: return default
    return default

def _save_json(path: Path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)

def load_used():      return _load_json(USED_FILE, {"used_ids": []})
def save_used(d):     _save_json(USED_FILE, d)
def load_schedule():  return _load_json(SCHEDULE_FILE, {"date": None, "slots": []})
def save_schedule(d): _save_json(SCHEDULE_FILE, d)

def ensure_today_schedule():
    today = datetime.now(PARIS_TZ).date().isoformat()
    sch = load_schedule()
    if sch.get("date") != today or not sch.get("slots"):
        random.seed()
        slots = []
        for h in SLOTS_HOURS:
            m = random.choice(MINUTES_GRID)
            slots.append({"hour": h, "minute": m, "posted": False})
        sch = {"date": today, "slots": slots}
        save_schedule(sch)
        picked = ", ".join(f"{s['hour']:02d}:{s['minute']:02d}" for s in slots)
        print(f"📅 Planning du {today} (Europe/Paris) → {picked}")
    return sch

def should_post_now(sch):
    now = datetime.now(PARIS_TZ)
    today = now.date()
    for slot in sch["slots"]:
        if slot.get("posted"): continue
        slot_dt = datetime(today.year, today.month, today.day, slot["hour"], slot["minute"], tzinfo=PARIS_TZ)
        if slot_dt <= now < (slot_dt + timedelta(minutes=GRACE_MINUTES)):
            delay = int((now - slot_dt).total_seconds() // 60)
            if delay > 0:
                print(f"⏱️ Créneau rattrapé avec {delay} min de retard (tolérance {GRACE_MINUTES} min).")
            return slot
    return None

def mark_posted(sch, slot):
    slot["posted"] = True
    save_schedule(sch)

def drive_service():
    sa_json = json.loads(base64.b64decode(SA_JSON_B64).decode("utf-8"))
    creds = Credentials.from_service_account_info(sa_json, scopes=["https://www.googleapis.com/auth/drive.readonly"])
    return build("drive", "v3", credentials=creds, cache_discovery=False)

def list_videos_in_folder(svc, folder_id: str) -> List[dict]:
    q = f"'{folder_id}' in parents and trashed=false"
    fields = "files(id,name,mimeType,size,modifiedTime),nextPageToken"
    page_token = None; out = []
    while True:
        resp = svc.files().list(q=q, spaces="drive", fields=f"nextPageToken,{fields}", pageToken=page_token).execute()
        out.extend(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token: break
    return [f for f in out if f["name"].lower().endswith((".mp4",".mov",".m4v",".webm"))]

def list_all_videos(svc) -> List[dict]:
    allv = []
    for fid in FOLDER_IDS:
        allv.extend(list_videos_in_folder(svc, fid))
    return allv

def pick_one(files: List[dict], used_ids: List[str]) -> dict | None:
    remaining = [f for f in files if f["id"] not in used_ids]
    if not remaining:
        used_ids.clear()
        remaining = files[:]
    random.shuffle(remaining)
    return remaining[0] if remaining else None

def download_file(svc, file_id: str, dest: Path):
    req = svc.files().get_media(fileId=file_id)
    fh = io.FileIO(dest, "wb")
    downloader = MediaIoBaseDownload(fh, req)
    done = False
    while not done:
        status, done = downloader.next_chunk()
        if status:
            print(f"Téléchargement {int(status.progress()*100)}%")

def run_youtube_upload(local_path: Path, title: str, desc: str, tags: list[str]):
    if "#Shorts" not in title and "#Shorts" not in desc and "#shorts" not in desc.lower():
        desc = (desc + " #Shorts").strip()
    cmd = [
        "youtube-upload",
        "--client-secrets", str(CLIENT_SECRETS),
        "--credentials-file", str(CREDENTIALS),
        "--title", title,
        "--description", desc,
        "--tags", ",".join(tags),
        "--category", "22",
        "--privacy", "public",
        str(local_path),
    ]
    print("RUN:", " ".join(cmd))
    subprocess.run(cmd, check=True)

def main():
    sch = ensure_today_schedule()
    now = datetime.now(PARIS_TZ)
    print(f"🫀 Passage cron: {now:%Y-%m-%d %H:%M:%S} (Europe/Paris)")

    slot = should_post_now(sch)
    if not slot and os.environ.get("FORCE_POST") == "1":
        slot = {"hour": 99, "minute": 99, "posted": False}
    if not slot:
        print(f"⏳ {now:%Y-%m-%d %H:%M} (Paris) — pas l'heure tirée. Prochain passage…")
        return

    print(f"🕒 Créneau déclenché: {slot['hour']:02d}:{slot['minute']:02d} (Europe/Paris)")

    used = load_used()
    svc = drive_service()
    files = list_all_videos(svc)
    if not files:
        print("Aucune vidéo trouvée dans le(s) dossier(s) Drive.")
        return

    chosen = pick_one(files, used["used_ids"])
    if not chosen:
        print("Toutes les vidéos disponibles ont été utilisées, on repartira de zéro la prochaine fois.")
        return

    print(f"🎯 Vidéo: {chosen['name']} ({chosen['id']})")

    tmpdir = Path(tempfile.mkdtemp())
    local = tmpdir / chosen["name"]
    print("⬇️ Téléchargement…"); download_file(svc, chosen["id"], local)

    title = chosen["name"].rsplit(".", 1)[0][:95]
    desc  = random.choice(DESCRIPTIONS)
    tags  = ["shorts", "fun", "fr"]

    try:
        run_youtube_upload(local, title, desc, tags)
        used["used_ids"].append(chosen["id"]); save_used(used)
        mark_posted(sch, slot)
        print("✅ Upload OK — état/plan du jour mis à jour.")
    except subprocess.CalledProcessError as e:
        print("❌ Upload échec:", e)

if __name__ == "__main__":
    random.seed()
    main()
