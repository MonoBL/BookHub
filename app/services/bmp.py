"""Image -> 8-bit grayscale BMP converter for e-readers (XTeink X4 etc).

Ported from to-bmp/convert-to-bmp.py. Each image is scaled to fill the target
size then centre-cropped (cover fit), converted to 8-bit grayscale, and saved
as BMP. One image -> a single .bmp; many images -> a .zip of .bmp files.

Runs in a thread (Pillow is blocking). Pure decode/encode, no network.
"""
import io
import logging
import zipfile
from pathlib import Path

from PIL import Image

log = logging.getLogger("bookhub")

# Pillow decoders we accept. Anything else is rejected before reaching here.
_ALLOWED = {"JPEG", "PNG", "BMP", "WEBP", "TIFF"}


def _convert_one(data: bytes, width: int, height: int, grayscale: bool) -> bytes:
    """Convert a single image's bytes into BMP bytes (cover-fit + centre-crop)."""
    with Image.open(io.BytesIO(data)) as im:
        if im.format not in _ALLOWED:
            raise ValueError(f"unsupported image format: {im.format}")
        # Honour EXIF orientation, then drop alpha to a flat background.
        im = im.convert("RGB") if im.mode in ("RGBA", "P", "LA") else im
        if grayscale:
            im = im.convert("L")

        w, h = im.size
        scale = max(width / w, height / h)
        new_w, new_h = max(1, int(w * scale)), max(1, int(h * scale))
        im = im.resize((new_w, new_h), Image.LANCZOS)
        left = (new_w - width) // 2
        top = (new_h - height) // 2
        im = im.crop((left, top, left + width, top + height))

        out = io.BytesIO()
        im.save(out, "BMP")
        return out.getvalue()


def convert_images_to_bmp(
    images: list[tuple[str, bytes]],
    dest: Path,
    width: int,
    height: int,
    grayscale: bool = True,
) -> str:
    """Convert images and write the result to `dest` (a directory).

    Writes either `<dest>/out.bmp` (one image) or `<dest>/out.zip` (many).
    Returns the produced extension ("bmp" or "zip"). Raises if nothing converts.
    """
    dest.mkdir(parents=True, exist_ok=True)
    results: list[tuple[str, bytes]] = []
    failures: list[str] = []

    for name, data in images:
        try:
            bmp = _convert_one(data, width, height, grayscale)
            stem = Path(name).stem or "image"
            results.append((f"{stem}.bmp", bmp))
        except Exception as exc:  # one bad image must not kill the batch
            log.warning("bmp_convert_failed name=%s err=%s", name, exc)
            failures.append(name)

    if not results:
        raise RuntimeError("No images could be converted")

    if len(results) == 1:
        out = dest / "out.bmp"
        out.write_bytes(results[0][1])
        return "bmp"

    out = dest / "out.zip"
    seen: dict[str, int] = {}
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for fname, content in results:
            # Disambiguate duplicate stems (a.jpg + a.png -> a.bmp, a-1.bmp).
            if fname in seen:
                seen[fname] += 1
                stem = Path(fname).stem
                fname = f"{stem}-{seen[fname]}.bmp"
            else:
                seen[fname] = 0
            zf.writestr(fname, content)
    return "zip"
