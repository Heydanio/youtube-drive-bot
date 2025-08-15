# youtube_runner.py — Google Drive → YouTube Shorts, 5 créneaux/jour (heure FR)
import base64, io, json, os, random, subprocess, sys, tempfile, re
from pathlib import Path
from typing import List
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

# =======================
#   CONFIG UTILISATEUR
# =======================
import random  # (en haut du fichier il est déjà importé, sinon garde-le)

# Gros pool de descriptions variées — YouTube détecte #Shorts dans titre/desc
DESCRIPTIONS = [
    "Clip rapide ⚡ #Shorts",
    "Best moment du jour 🎯 #Shorts",
    "Tu t’y attendais pas 😅 #Shorts",
    "Insolite mais vrai 🤯 #Shorts",
    "Petit shot de dopamine ⚡ #Shorts",
    "Ça régale 🔥 #Shorts",
    "Moment satisfaisant ✨ #Shorts",
    "Tu valides ? 👀 #Shorts",
    "C’était obligé 😂 #Shorts",
    "On en parle ? 🤔 #Shorts",
    "Tellement vrai… 😭 #Shorts",
    "Coup de pression 😮‍💨 #Shorts",
    "Le move qu’il fallait 💪 #Shorts",
    "Ça part trop loin 🤡 #Shorts",
    "T’as déjà vu ça ? 👇 #Shorts",
    "Inattendu jusqu’à la fin 👇 #Shorts",
    "Rage quit imminent 😤 #Shorts",
    "Montage express 🎬 #Shorts",
    "Compilation instantanée ⚡ #Shorts",
    "Le clutch parfait 🧠 #Shorts",
    "Ça passe crème 😎 #Shorts",
    "Try not to laugh 😆 #Shorts",
    "Le karma instantané ☄️ #Shorts",
    "On refait ? 🙃 #Shorts",
    "C’est validé ou pas ? ✅ #Shorts",
    "Moment magique ✨ #Shorts",
    "Ça part en vrille 😂 #Shorts",
    "Le détail qui tue 👀 #Shorts",
    "POV: tu découvres ça 🤯 #Shorts",
    "Propre et sans bavure 🧼 #Shorts",
    "Stop au scroll 👉 regarde ça #Shorts",
    "Le meilleur passage 😍 #Shorts",
    "J’en reviens pas 😳 #Shorts",
    "En une prise 🔥 #Shorts",
    "Le combo parfait 🧩 #Shorts",
    "Tu t’y attendais ? 😏 #Shorts",
    "Moment culte 🤌 #Shorts",
    "Ça devrait être illégal 😅 #Shorts",
    "Tu like, tu partages 🙏 #Shorts",
    "On en fait un autre ? 🤝 #Shorts",
]

# Gros pool de tags (sans # — YouTube tags sont des mots-clés)
TAGS_POOL = [
    "shorts","humour","drôle","fun","fr","tendance","viral","meme","montage","clip",
    "gaming","stream","twitch","moments","compilation","edit","capcut","reaction","lol","wtf",
    "trend","bestof","france","entertainment","amusant","buzz","highlight","clutch","fails","win",
    "asmr","music","beat","challenge","ironie","parodie","sketch","storytime","live","popculture",
    "anime","manga","film","serie","geek","setup","tips","astuces","howto","inspiration"
]

DEFAULT_PRIVACY = "public"          # public | unlisted | private
YOUTUBE_CATEGORY_NAME = "Entertainment"   # utiliser le NOM de la catégorie

def pick_tags(pool, min_n=3, max_n=8):
    """Sélectionne un nombre aléatoire de tags uniques depuis le pool."""
    n = random.randint(min_n, max_n)
    n = min(n, len(pool))
    return random.sample(pool, n)

# =======================
#   ETAT LOCAL (versionné)
# =======================
USED_FILE     = Path("state/yt_used.json")
SCHEDULE_FILE = Path("state/yt_schedule.json")

# =======================
#   CRENEAUX JOURNALIERS
# =======================
PARIS_TZ      = ZoneInfo("Europe/Paris")
SLOTS_HOURS   = [8, 11, 14, 17, 20]
MINUTES_GRID  = list(range(0, 60, 5))
GRACE_MINUTES = 10

# =======================
#   SECRETS / ENV
# =======================
FOLDER_IDS   = [s.strip() for s in os.environ["GDRIVE_FOLDER_IDS"].split(",") if s.strip()]
SA_JSON_B64  = os.environ["GDRIVE_SA_JSON_B64"]

# =======================
#   UTILS
# =======================
def _load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return default
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
    now = datetime.now(PARIS_TZ)
    today = now.date().isoformat()
    sch = load_schedule()
    if sch.get("date") != today or not sch.get("slots"):
        random.seed()
        slots = []
        for h in SLOTS_HOURS:
            m = random.choice(MINUTES_GRID)
            slots.append({"hour": h, "minute": m, "posted": False})
        sch = {"date": today, "slots": slots}
        save_schedule(sch)
        print(f"📅 Planning du {today} (Europe/Paris) → " + ", ".join(f"{s['hour']:02d}:{s['minute']:02d}" for s in slots))
    return sch

def should_post_now(sch):
    now = datetime.now(PARIS_TZ)
    today = now.date()
    for slot in sch["slots"]:
        if slot.get("posted"):
            continue
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

# =======================
#   GOOGLE DRIVE
# =======================
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
    return [f for f in out if f["name"].lower().endswith((".mp4", ".mov", ".m4v", ".webm"))]

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

# =======================
#   YOUTUBE UPLOAD
# =======================
TITLE_MAX = 100
def sanitize_title(name: str) -> str:
    name_no_ext = re.sub(r"\.[A-Za-z0-9]{2,4}$", "", name)
    title = re.sub(r"\s+", " ", name_no_ext).strip()
    if len(title) > TITLE_MAX:
        title = title[:TITLE_MAX-1] + "…"
    return title

def run_upload(local_path: Path, title: str, description: str, tags: List[str]):
    base_cmd = [
        "youtube-upload",
        "--client-secrets", "client_secrets.json",
        "--credentials-file", "youtube_credentials.json",
        "--title", title,
        "--description", description,
        "--tags", ",".join(tags),
        "--privacy", DEFAULT_PRIVACY,
        str(local_path),
    ]

    # 1) tentative avec catégorie par NOM
    cmd1 = base_cmd.copy()
    cmd1[0:0] = []  # no-op lisible
    cmd1.insert(7, YOUTUBE_CATEGORY_NAME)         # valeur
    cmd1.insert(7, "--category")                  # option
    print("RUN (with category name):", " ".join(cmd1))
    try:
        subprocess.run(cmd1, check=True)
        return
    except subprocess.CalledProcessError as e:
        print("⚠️ Échec avec catégorie par nom:", e)

    # 2) tentative sans catégorie (fallback)
    cmd2 = base_cmd
    print("RUN (fallback without category):", " ".join(cmd2))
    subprocess.run(cmd2, check=True)

# =======================
#   MAIN
# =======================
def main():
    now = datetime.now(PARIS_TZ)
    sch = ensure_today_schedule()
    print(f"🫀 Passage cron: {now:%Y-%m-%d %H:%M:%S} (Europe/Paris)")
    slot = should_post_now(sch)

    if not slot and os.environ.get("FORCE_POST") == "1":
        slot = {"hour": 99, "minute": 99, "posted": False}

    if not slot:
        print(f"⏳ {now:%Y-%m-%d %H:%M} (Paris) — pas l'heure tirée aujourd'hui. Prochain passage…")
        return

    print(f"🕒 Créneau déclenché: {slot['hour']:02d}:{slot['minute']:02d} (Europe/Paris)")

    used = load_used()
    svc = drive_service()
    files = list_all_videos(svc)
    if not files:
        print("Aucune vidéo trouvée dans le(s) dossier(s) Drive.")
        return

    chosen = pick_one(files, used["used_ids"])
    print(f"🎯 Vidéo: {chosen['name']} ({chosen['id']})")

    tmpdir = Path(tempfile.mkdtemp())
    local = tmpdir / chosen["name"]
    print("⬇️ Téléchargement…"); download_file(svc, chosen["id"], local)

    title = sanitize_title(chosen["name"])
    desc = random.choice(DESCRIPTIONS)
    tags = pick_tags(TAGS_POOL, 4, 10)  # par ex. 4 à 10 tags à chaque fois
    print(f"📝 Titre: {title}")
    print(f"📝 Description: {desc}")
    print(f"🏷️ Tags: {', '.join(tags)}")

    try:
        run_upload(local, title, desc, tags)
        used["used_ids"].append(chosen["id"]); save_used(used)
        mark_posted(sch, slot)
        print("✅ Upload OK — état/plan du jour mis à jour.")
    except subprocess.CalledProcessError as e:
        print("❌ Upload échec:", e)

if __name__ == "__main__":
    random.seed()
    main()
