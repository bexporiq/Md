#!/usr/bin/env python3
"""Build a complete MDAV1 artwork vector catalog from the real artwork manifest.

Supports both:
  * full manifest: {"cards": {card_id: {art_id: {"bestArt": ..., "idx": {"ko": ...}}}}}
  * normalized app index: {card_id: [{"art": ..., "ko": ..., "best": ...}]}

Successful vectors are cached individually, so reruns only download missing/invalid entries.
The final binary is published only when every indexed artwork has a valid vector.
"""
from __future__ import annotations
import concurrent.futures
import io
import json
import math
import os
import struct
import time
from pathlib import Path
from urllib.request import Request, urlopen

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "app/src/main/assets/artwork_manifest_index.json"
OUTPUT = ROOT / "app/src/main/assets/artwork_vectors_v1.bin"
CACHE = ROOT / ".artwork_vector_cache"
FAILURES = ROOT / "artwork_vector_failures.json"
MAGIC = b"MDAV1\0\0\0"
DIM = 192
SIZE = 64
RECORD = 4 + 2 + DIM * 4


def _paths(value):
    """Normalize a manifest path or language path list to strings."""
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, list):
        out = []
        for v in value:
            out.extend(_paths(v))
        return out
    if isinstance(value, dict):
        # Some manifest revisions wrap language paths in objects.
        for key in ("url", "path", "src"):
            if key in value:
                return _paths(value[key])
    return []


def _url(path):
    if not path:
        return None
    if path.startswith("//"):
        return "https:" + path
    if path.startswith("/"):
        return "https://artworks.ygoresources.com" + path
    if path.startswith("http://"):
        return "https://" + path[len("http://"):]
    return path if path.startswith("https://") else None


def choose_urls(item):
    """Korean artwork first; bestArt/best is the fallback."""
    korean, fallback = [], []
    idx = item.get("idx")
    if isinstance(idx, dict):
        korean += _paths(idx.get("ko"))
    # Already-normalized app index.
    korean += _paths(item.get("ko"))
    fallback += _paths(item.get("bestArt"))
    fallback += _paths(item.get("best"))
    result = []
    for path in korean + fallback:
        u = _url(path)
        if u and u not in result:
            result.append(u)
    return result


def iter_entries(document):
    cards = document.get("cards") if isinstance(document, dict) else None
    # Full upstream manifest is wrapped in a top-level "cards" property.
    source = cards if isinstance(cards, dict) else document
    if not isinstance(source, dict):
        raise ValueError("Manifest must be a JSON object or contain a cards object")
    entries = []
    for raw_cid, arts in source.items():
        try:
            cid = int(raw_cid)
        except (TypeError, ValueError):
            continue
        if isinstance(arts, dict):
            iterable = arts.items()
        elif isinstance(arts, list):
            iterable = ((item.get("art", i), item) for i, item in enumerate(arts))
        else:
            continue
        for raw_art, item in iterable:
            if not isinstance(item, dict):
                continue
            try:
                art = int(raw_art if raw_art is not None else item.get("art", 0))
            except (TypeError, ValueError):
                art = 0
            urls = choose_urls(item)
            if urls:
                entries.append((cid, art, urls))
    entries.sort(key=lambda x: (x[0], x[1]))
    # The manifest must not silently collapse duplicate card/art keys.
    keys = [(c, a) for c, a, _ in entries]
    if len(keys) != len(set(keys)):
        raise ValueError("Duplicate card ID/artwork ID pair in manifest")
    if not entries:
        raise ValueError("No usable artwork paths found in manifest")
    return entries


def fetch(urls):
    errors = []
    for url in urls:
        for attempt in range(4):
            try:
                req = Request(url, headers={"User-Agent": "MDTracker-ArtworkIndexer/2.0"})
                with urlopen(req, timeout=35) as response:
                    data = response.read(8 * 1024 * 1024 + 1)
                if len(data) > 8 * 1024 * 1024:
                    raise ValueError("image exceeds 8 MiB safety cap")
                # Verify that the response is a decodable image before accepting it.
                with Image.open(io.BytesIO(data)) as im:
                    im.verify()
                return data, url
            except Exception as exc:
                errors.append(f"{url} [{attempt + 1}/4]: {type(exc).__name__}: {exc}")
                if attempt < 3:
                    time.sleep(min(2 ** attempt, 8))
    raise RuntimeError(" | ".join(errors))


def hsv(r, g, b):
    mx, mn = max(r, g, b), min(r, g, b)
    d = mx - mn
    if d == 0: h = 0.0
    elif mx == r: h = ((g - b) / d) % 6
    elif mx == g: h = (b - r) / d + 2
    else: h = (r - g) / d + 4
    return h * 60.0, (0.0 if mx == 0 else d / mx), mx / 255.0


def descriptor(data):
    im = Image.open(io.BytesIO(data)).convert("RGB").resize(
        (SIZE, SIZE), Image.Resampling.BILINEAR
    )
    px = list(im.getdata())
    gray = [0.299*r/255 + 0.587*g/255 + 0.114*b/255 for r, g, b in px]
    v = [0.0] * DIM
    hist = [[0.0] * 16 for _ in range(3)]
    lum = [0.0] * 16
    counts = [0] * 16
    for y in range(SIZE):
        for x in range(SIZE):
            r, g, b = px[y * SIZE + x]
            h, s, val = hsv(r, g, b)
            hist[0][min(15, int(h / 360 * 16))] += 1
            hist[1][min(15, int(s * 16))] += 1
            hist[2][min(15, int(val * 16))] += 1
            cell = (y // 16) * 4 + (x // 16)
            lum[cell] += gray[y * SIZE + x]
            counts[cell] += 1
    for y in range(1, SIZE - 1):
        for x in range(1, SIZE - 1):
            i = y * SIZE + x
            gx = gray[i + 1] - gray[i - 1]
            gy = gray[i + SIZE] - gray[i - SIZE]
            mag = math.sqrt(gx * gx + gy * gy)
            if mag < 0.015:
                continue
            angle = math.degrees(math.atan2(gy, gx)) % 180
            cell = (y // 16) * 4 + (x // 16)
            v[cell * 8 + min(7, int(angle / 22.5))] += mag
    for k, histogram in enumerate(hist):
        total = max(1.0, sum(histogram))
        for i, value in enumerate(histogram):
            v[128 + k * 16 + i] = value / total
    for i in range(16):
        v[176 + i] = lum[i] / max(1, counts[i])
    for start, length in ((0, 128), (128, 64)):
        norm = math.sqrt(sum(x*x for x in v[start:start+length])) or 1e-8
        for i in range(start, start + length):
            v[i] /= norm
    norm = math.sqrt(sum(x*x for x in v)) or 1e-8
    return [x / norm for x in v]


def cache_path(cid, art):
    return CACHE / f"{cid}_{art}.f32"


def read_cached(path):
    try:
        raw = path.read_bytes()
        if len(raw) != DIM * 4:
            return None
        vec = struct.unpack("<192f", raw)
        if not all(math.isfinite(x) for x in vec):
            return None
        return vec
    except (OSError, struct.error):
        return None


def process_one(entry):
    cid, art, urls = entry
    cp = cache_path(cid, art)
    cached = read_cached(cp)
    if cached is not None:
        return (cid, art, cached, None, "cache")
    data, used_url = fetch(urls)
    vec = descriptor(data)
    cp.parent.mkdir(parents=True, exist_ok=True)
    tmp = cp.with_suffix(".f32.part")
    tmp.write_bytes(struct.pack("<192f", *vec))
    tmp.replace(cp)
    return (cid, art, vec, None, used_url)


def main():
    if not MANIFEST.is_file():
        raise FileNotFoundError(f"Missing manifest index: {MANIFEST}")
    document = json.loads(MANIFEST.read_text(encoding="utf-8"))
    entries = iter_entries(document)
    CACHE.mkdir(parents=True, exist_ok=True)
    print(f"Manifest artwork entries: {len(entries)}", flush=True)

    results = {}
    failures = []
    completed = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(process_one, entry): entry for entry in entries}
        for future in concurrent.futures.as_completed(futures):
            cid, art, urls = futures[future]
            try:
                c, a, vec, _, source = future.result()
                results[(c, a)] = vec
                completed += 1
            except Exception as exc:
                failures.append({
                    "card_id": cid, "artwork_id": art,
                    "urls_tried": urls,
                    "error": f"{type(exc).__name__}: {exc}"
                })
            done = completed + len(failures)
            if done % 250 == 0 or done == len(entries):
                print(f"Processed {done}/{len(entries)}; vectors={completed}; failed={len(failures)}", flush=True)

    if failures:
        FAILURES.write_text(json.dumps(failures, ensure_ascii=False, indent=2), encoding="utf-8")
        raise RuntimeError(
            f"{len(failures)} artwork downloads failed. Partial vectors are cached; "
            "rerun this workflow to retry only missing/failed entries. No final catalog was published."
        )

    # Deterministic output order is important for reproducibility.
    tmp = OUTPUT.with_suffix(".bin.part")
    with tmp.open("wb") as out:
        out.write(MAGIC)
        out.write(struct.pack("<I", len(entries)))
        for cid, art, _ in entries:
            vec = results[(cid, art)]
            out.write(struct.pack("<ih192f", cid, art, *vec))
        out.flush()
        os.fsync(out.fileno())
    expected_size = 12 + len(entries) * RECORD
    if tmp.stat().st_size != expected_size:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"Binary size mismatch: {tmp.stat().st_size} != {expected_size}")
    tmp.replace(OUTPUT)
    FAILURES.unlink(missing_ok=True)
    print(f"SUCCESS: wrote {len(entries)} vectors ({OUTPUT.stat().st_size:,} bytes)", flush=True)


if __name__ == "__main__":
    main()
