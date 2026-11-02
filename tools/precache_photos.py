# tools/precache_photos.py
import os
import sys
import time
from pathlib import Path
from PIL import Image

src_dir = Path(r"F:\Shares\Common Share Files\userimageprofiles")
cache_dir = Path(r"c:\Users\admin\Desktop\Student_Browsing_History_Analysis_System\data\photo_cache")
cache_dir.mkdir(parents=True, exist_ok=True)

start = time.time()
created = 0
skipped = 0
failed = 0

if not src_dir.exists():
    print(f"Source dir {src_dir} not found!")
    sys.exit(1)

for trno_dir in src_dir.iterdir():
    if not trno_dir.is_dir():
        continue
    trno = trno_dir.name.strip()
    if not trno.isdigit():
        continue
    
    out_file = cache_dir / f"{trno}.jpg"
    if out_file.exists() and out_file.stat().st_size > 0:
        skipped += 1
        continue
    
    # Look for candidate photo
    photo_candidate = None
    for cand in ("myloginphoto.png", "myloginphoto.jpg", "myloginphoto.jpeg", f"{trno}.png", f"{trno}.jpg"):
        p = trno_dir / cand
        if p.exists() and p.is_file():
            photo_candidate = p
            break
            
    if not photo_candidate:
        continue
        
    try:
        with Image.open(photo_candidate) as img:
            img = img.convert("RGB")
            img.thumbnail((200, 200), Image.Resampling.LANCZOS)
            img.save(out_file, format="JPEG", quality=85)
        created += 1
    except Exception as e:
        failed += 1

elapsed = round(time.time() - start, 2)
total = len(list(cache_dir.glob("*.jpg")))
print(f"Pre-caching complete in {elapsed}s: {created} new generated, {skipped} already cached, {total} total thumbnails in cache.")

