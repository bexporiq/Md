#!/usr/bin/env python3
"""Strict pre-build validation for the MDAV1 artwork catalog (stdlib only)."""
import json, math, struct, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "app/src/main/assets/artwork_manifest_index.json"
BINARY = ROOT / "app/src/main/assets/artwork_vectors_v1.bin"
MAGIC = b"MDAV1\0\0\0"
DIM = 192
RECORD = 6 + DIM * 4

def expected_keys(doc):
    source = doc.get("cards") if isinstance(doc, dict) and isinstance(doc.get("cards"), dict) else doc
    keys = []
    for cid, arts in source.items():
        try: card_id = int(cid)
        except (TypeError, ValueError): continue
        iterable = arts.items() if isinstance(arts, dict) else (
            ((x.get("art", i), x) for i, x in enumerate(arts)) if isinstance(arts, list) else ()
        )
        for art_id, item in iterable:
            if not isinstance(item, dict): continue
            ko = item.get("ko")
            idx = item.get("idx")
            best = item.get("best") or item.get("bestArt")
            has_ko = bool(ko) or (isinstance(idx, dict) and bool(idx.get("ko")))
            if has_ko or best:
                try: keys.append((card_id, int(art_id if art_id is not None else item.get("art", 0))))
                except (TypeError, ValueError): pass
    return sorted(set(keys))

def main():
    if not MANIFEST.is_file(): raise SystemExit(f"FAIL: missing {MANIFEST}")
    if not BINARY.is_file(): raise SystemExit(f"FAIL: missing {BINARY}; full vector catalog has not been generated")
    doc = json.loads(MANIFEST.read_text(encoding="utf-8"))
    expected = expected_keys(doc)
    raw = BINARY.read_bytes()
    if len(raw) < 12 or raw[:8] != MAGIC: raise SystemExit("FAIL: invalid MDAV1 header")
    count = struct.unpack_from("<I", raw, 8)[0]
    if count != len(expected): raise SystemExit(f"FAIL: vector count {count} does not match manifest count {len(expected)}")
    if len(raw) != 12 + count * RECORD: raise SystemExit("FAIL: binary byte length does not match header count")
    offset = 12
    actual = []
    for _ in range(count):
        cid, art = struct.unpack_from("<ih", raw, offset)
        offset += 6
        vec = struct.unpack_from("<192f", raw, offset)
        offset += DIM * 4
        if not all(math.isfinite(x) for x in vec): raise SystemExit(f"FAIL: non-finite vector at {cid}/{art}")
        norm = math.sqrt(sum(x*x for x in vec))
        if not 0.98 <= norm <= 1.02: raise SystemExit(f"FAIL: vector norm {norm:.4f} at {cid}/{art}")
        actual.append((cid, art))
    if actual != expected: raise SystemExit("FAIL: binary card/artwork IDs do not exactly match manifest index")
    print(f"VALID: {count} vectors, {len(raw):,} bytes; all IDs and vectors verified")

if __name__ == "__main__":
    main()
