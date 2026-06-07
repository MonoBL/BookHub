"""Convert JPEGs in this folder to 8-bit grayscale BMP for the XTeink X4.

Skips any JPEG that already has a matching .bmp ("unturned" only).
"""
from pathlib import Path
from PIL import Image

folder = Path(__file__).resolve().parent
converted = skipped = failed = 0

jpegs = sorted(set(folder.glob("*.jpg")) | set(folder.glob("*.jpeg")))

for src in jpegs:
    dst = src.with_suffix(".bmp")
    if dst.exists():
        print(f"SKIP  {src.name}  -- .bmp already exists")
        skipped += 1
        continue
    try:
        with Image.open(src) as im:
            im = im.convert("L")
            # Scale to fill 480x800, then centre-crop
            target_w, target_h = 480, 800
            w, h = im.size
            scale = max(target_w / w, target_h / h)
            new_w, new_h = int(w * scale), int(h * scale)
            im = im.resize((new_w, new_h), Image.LANCZOS)
            left = (new_w - target_w) // 2
            top = (new_h - target_h) // 2
            im = im.crop((left, top, left + target_w, top + target_h))
            im.save(dst, "BMP")
        print(f"CONV  {src.name}  -->  {dst.name}")
        converted += 1
    except Exception as e:
        print(f"FAIL  {src.name}  -- {e}")
        failed += 1

print()
print(f"Done. Converted {converted}, skipped {skipped}, failed {failed}.")
