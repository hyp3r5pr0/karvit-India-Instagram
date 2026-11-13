#!/usr/bin/env python3
"""Build reels and carousel slides from product photos (content/build.yml).

Runs in GitHub Actions ("Build media" workflow); also works locally with
internet access and ffmpeg installed.

  python tools/build_media.py                 # build everything in build.yml
  python tools/build_media.py --only chai     # just outputs whose path contains "chai"
  python tools/build_media.py --skip-existing # don't rebuild files already in media/
"""

from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import sys
import time
from pathlib import Path

import requests
import yaml
from PIL import Image, ImageDraw, ImageFilter

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import ROOT, font, text_block, wordmark  # noqa: E402
from prep_image import prep  # noqa: E402

BUILD_FILE = ROOT / "content" / "build.yml"
CACHE = ROOT / ".cache" / "photos"
PREVIEWS = ROOT / "media" / "previews"
BRAND_BG = (24, 24, 27)
ACCENT = (232, 168, 56)   # warm marigold, fits the desi + Diwali designs
SIZE_TOKEN = re.compile(r"/il_[0-9a-zA-Z]+x[0-9a-zA-Z]+\.")


def candidates(url: str) -> list[str]:
    """Etsy links: try the biggest size first. Any other link is used as is."""
    if not SIZE_TOKEN.search(url):
        return [url]
    out = []
    for size in ("fullxfull", "1588xN", "1080xN", "794xN"):
        u = SIZE_TOKEN.sub(f"/il_{size}.", url, count=1)
        if u not in out:
            out.append(u)
    if url not in out:
        out.append(url)
    return out


def fetch(name: str, url: str, *, http=requests) -> Path:
    if not url.startswith(("http://", "https://")):
        local = ROOT / url   # a file in this repo, e.g. assets/cards/poll.png
        if not local.exists():
            sys.exit(f"Photo '{name}': file not found in repo: {url}")
        return local
    CACHE.mkdir(parents=True, exist_ok=True)
    dest = CACHE / f"{name}-{hashlib.sha1(url.encode()).hexdigest()[:8]}.jpg"
    if dest.exists():
        return dest
    errors = []
    headers = {"User-Agent": "Mozilla/5.0 (KARVIT media builder; shop owner)"}
    for u in candidates(url):
        for attempt in range(2):
            try:
                r = http.get(u, headers=headers, timeout=60)
                if r.status_code == 200 and r.content[:3] in (b"\xff\xd8\xff", b"\x89PN", b"RIF"):
                    dest.write_bytes(r.content)
                    with Image.open(dest) as im:
                        print(f"  {name}: {im.width}x{im.height}  ({u.split('/il_')[-1][:12]}…)")
                    return dest
                errors.append(f"{u} → HTTP {r.status_code}")
                break
            except requests.RequestException as exc:
                errors.append(f"{u} → {exc}")
                time.sleep(3)
    sys.exit(f"Couldn't download photo '{name}':\n  " + "\n  ".join(errors[-4:]))


def cover_slide(out: Path, title: str, sub: str | None) -> None:
    W, H = 1080, 1350
    im = Image.new("RGB", (W, H), BRAND_BG)
    wordmark(im, y=110, size=38)
    text_block(im, title, center_y=H // 2 - 40, size=104, max_w=900, pill=False)
    if sub:
        text_block(im, sub, center_y=H - 230, size=46, max_w=900, fill=(20, 20, 20),
                   pill_rgba=ACCENT + (255,))
    out.parent.mkdir(parents=True, exist_ok=True)
    im.save(out, "JPEG", quality=92, optimize=True, progressive=True)


def reel_preview(video: Path, dest: Path) -> None:
    """4 frames side by side, so the reel can be checked on a phone."""
    dur = float(subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
         str(video)], capture_output=True, text=True).stdout.strip() or 0)
    dest.parent.mkdir(parents=True, exist_ok=True)
    frames = []
    for i, frac in enumerate((0.12, 0.38, 0.62, 0.95)):
        f = dest.with_suffix(f".f{i}.png")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{dur * frac:.2f}", "-i", str(video),
                        "-frames:v", "1", "-vf", "scale=360:-1", str(f)], check=True)
        frames.append(f)
    _montage([Image.open(f) for f in frames], dest)
    for f in frames:
        f.unlink()


def _montage(images: list[Image.Image], dest: Path) -> None:
    gap = 12
    h = max(i.height for i in images)
    sheet = Image.new("RGB", (sum(i.width for i in images) + gap * (len(images) - 1), h), "white")
    x = 0
    for i in images:
        sheet.paste(i, (x, 0))
        x += i.width + gap
    dest.parent.mkdir(parents=True, exist_ok=True)
    sheet.convert("RGB").save(dest, "JPEG", quality=85)


def build(only: str | None, skip_existing: bool, *, fetcher=fetch) -> list[Path]:
    spec = yaml.safe_load(BUILD_FILE.read_text(encoding="utf-8"))
    photos: dict[str, str] = spec.get("photos") or {}
    derived: dict[str, dict] = spec.get("derived") or {}
    made: list[Path] = []

    def make_derived(name: str) -> Path:
        """A crop of another photo: box = [x0, y0, x1, y1] as fractions of that
        photo; hide = boxes (same units) to blur out, e.g. text that gives away a
        quiz answer; mark = text drawn on each hidden box. Upscaled so close-ups
        fill the slide instead of sitting small in the middle."""
        d = derived[name]
        base = photo(str(d["from"]))
        key = hashlib.sha1(repr((str(base), sorted(d.items()))).encode()).hexdigest()[:8]
        dest = CACHE / f"derived-{name}-{key}.png"
        if dest.exists():
            return dest
        with Image.open(base) as im0:
            im = im0.convert("RGB")
        w, h = im.size
        x0, y0, x1, y1 = (float(v) for v in d["box"])
        box = (int(x0 * w), int(y0 * h), int(x1 * w), int(y1 * h))
        for hb in d.get("hide") or []:
            hx0, hy0, hx1, hy1 = (float(v) for v in hb)
            r = (int(hx0 * w), int(hy0 * h), int(hx1 * w), int(hy1 * h))
            # paint over with the darkest tone around it (the tee), soft edges
            region = im.crop(r).convert("L")
            hist = region.histogram()
            cut, seen = 0, 0
            for level, count in enumerate(hist):
                seen += count
                if seen >= 0.2 * sum(hist):
                    cut = level
                    break
            px = [p for p, g in zip(im.crop(r).getdata(), region.getdata()) if g <= cut] or [(18, 18, 20)]
            tone = tuple(sum(c[i] for c in px) // len(px) for i in range(3))
            mask = Image.new("L", im.size, 0)
            ImageDraw.Draw(mask).rounded_rectangle(r, radius=int(0.012 * w), fill=255)
            mask = mask.filter(ImageFilter.GaussianBlur(max(3, int(0.006 * w))))
            im = Image.composite(Image.new("RGB", im.size, tone), im, mask)
            if d.get("mark"):
                dd = ImageDraw.Draw(im)
                f = font(max(18, int((r[3] - r[1]) * 0.7)))
                tw = dd.textlength(str(d["mark"]), font=f)
                dd.text(((r[0] + r[2] - tw) / 2, r[1] + (r[3] - r[1]) * 0.1), str(d["mark"]),
                        font=f, fill=ACCENT)
        out = im.crop(box)
        scale = 1400 / max(out.size)
        if scale > 1:
            out = out.resize((round(out.width * scale), round(out.height * scale)), Image.LANCZOS)
            out = out.filter(ImageFilter.UnsharpMask(radius=2, percent=60, threshold=2))
        CACHE.mkdir(parents=True, exist_ok=True)
        out.save(dest)
        print(f"  {name}: crop of {d['from']} -> {out.width}x{out.height}")
        return dest

    def photo(ref: str) -> Path:
        """'name' = the photo; 'name@detail' = 2x close-up of the centre (the
        print on a model shot); 'name@crop:cx,cy,scale' = custom close-up, e.g.
        @crop:0.5,0.45,0.4 keeps 40% of the width around x=50%, y=45%.
        A name listed under derived: is a ready-made crop (see make_derived)."""
        name, _, mod = ref.partition("@")
        if name in derived and not mod:
            return make_derived(name)
        if name not in photos:
            sys.exit(f"build.yml: photo '{name}' is not listed under photos: or derived:")
        src = fetcher(name, photos[name])
        if not mod:
            return src
        if mod == "detail":
            cx, cy, scale = 0.5, 0.5, 0.5
        elif mod.startswith("crop:"):
            try:
                cx, cy, scale = (float(v) for v in mod[5:].split(","))
            except ValueError:
                sys.exit(f"build.yml: bad crop in '{ref}' (use @crop:0.5,0.45,0.4)")
        else:
            sys.exit(f"build.yml: unknown modifier in '{ref}' (use @detail or @crop:…)")
        dest = src.with_name(f"{src.stem}-{mod.replace(':', '_').replace(',', '_')}.jpg")
        if not dest.exists():
            with Image.open(src) as im:
                w, h = im.size
                side = scale * min(w, h)
                left = min(max(cx * w - side / 2, 0), w - side)
                top = min(max(cy * h - side / 2, 0), h - side)
                im.convert("RGB").crop((int(left), int(top), int(left + side),
                                        int(top + side))).save(dest, quality=94)
        return dest

    for reel in spec.get("reels") or []:
        out = ROOT / reel["out"]
        if (only and only not in reel["out"]) or (skip_existing and out.exists()):
            continue
        print(f"Reel → {reel['out']}")
        srcs = [photo(n) for n in reel["photos"]]
        cmd = [sys.executable, str(ROOT / "tools" / "make_reel.py"), *map(str, srcs),
               "--out", str(out)]
        if reel.get("hook"):
            cmd += ["--hook", reel["hook"]]
        for t in reel.get("texts") or []:
            cmd += ["--text", t]
        if "cta" in reel:
            cmd += ["--cta", reel["cta"] or ""]
        if reel.get("seconds"):
            cmd += ["--seconds", str(reel["seconds"])]
        if reel.get("music"):
            cmd += ["--music", str(ROOT / reel["music"])]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            sys.exit(f"make_reel failed for {reel['out']}:\n{res.stderr or res.stdout}")
        reel_preview(out, PREVIEWS / f"{out.stem}.jpg")
        made.append(out)

    for car in spec.get("carousels") or []:
        outdir = ROOT / car["outdir"]
        if only and only not in car["outdir"]:
            continue
        print(f"Carousel → {car['outdir']}")
        slides: list[Path] = []
        if car.get("cover"):
            c = car["cover"]
            dest = outdir / c["file"]
            if not (skip_existing and dest.exists()):
                cover_slide(dest, c["title"], c.get("sub"))
            slides.append(dest)
        for s in car.get("slides") or []:
            dest = outdir / s["file"]
            if not (skip_existing and dest.exists()):
                prep(photo(s["photo"]), dest, "portrait", None, s.get("text"), 0.04)
            slides.append(dest)
        _montage([Image.open(p).resize((270, 338)) for p in slides],
                 PREVIEWS / f"{outdir.name}.jpg")
        made += slides

    for fl in spec.get("flickers") or []:
        out = ROOT / fl["out"]
        if (only and only not in fl["out"]) or (skip_existing and out.exists()):
            continue
        print(f"Flicker → {fl['out']}")
        flicker_reel(fl, [photo(n) for n in fl["photos"]], out)
        reel_preview(out, PREVIEWS / f"{out.stem}.jpg")
        made.append(out)
    return made


def secret_card(code: str, line: str, size: tuple[int, int]) -> Image.Image:
    """The hidden frame of a pause challenge: a discount code on a brand card."""
    w, h = size
    im = Image.new("RGB", size, BRAND_BG)
    d = ImageDraw.Draw(im)
    d.rectangle([0, 0, w - 1, h - 1], outline=ACCENT, width=max(6, w // 90))
    wordmark(im, y=int(h * 0.12), size=int(w * 0.05))
    for text, y, fsize, fill in ((code, 0.38, 0.17, ACCENT), (line, 0.58, 0.065, (245, 243, 238)),
                                 ("use it at checkout · karvit.in", 0.70, 0.045, (170, 170, 175))):
        f = font(int(w * fsize))
        tw = d.textlength(text, font=f)
        d.text(((w - tw) / 2, h * y), text, font=f, fill=fill)
    return im


def flicker_reel(fl: dict, photos: list[Path], out: Path) -> None:
    """'Only 1% can pause at the right time': the photos flash past a few frames
    each and one moment shows a secret card (e.g. a discount code)."""
    from make_reel import compose_slide  # same layout as the other reels
    import tempfile
    fps = 30
    per = int(fl.get("frames_per_photo", 3))
    total = int(float(fl.get("seconds", 12)) * fps)
    sec = fl.get("secret") or {}
    s_at, s_len = int(float(sec.get("at", 7.3)) * fps), int(sec.get("frames", 2))
    with Image.open(photos[0]) as p0:
        card_size = p0.size
    with tempfile.TemporaryDirectory() as tmp:
        tmpd = Path(tmp)
        frames = []
        for i, src in enumerate(photos):
            f = tmpd / f"p{i}.png"
            compose_slide(src, top_text=fl.get("hook"), cta=fl.get("cta"), bg=None,
                          brand="KARVIT").save(f)
            frames.append(f)
        card = tmpd / "card.png"
        secret_card(str(sec.get("code", "PAUSE10")), str(sec.get("line", "10% OFF")),
                    card_size).save(tmpd / "card-src.png")
        compose_slide(tmpd / "card-src.png", top_text=fl.get("hook"), cta=fl.get("cta"),
                      bg=None, brand="KARVIT").save(card)
        seq = [card if s_at <= t < s_at + s_len else frames[(t // per) % len(frames)]
               for t in range(total)]
        runs: list[list] = []
        for f in seq:
            if runs and runs[-1][0] == f:
                runs[-1][1] += 1
            else:
                runs.append([f, 1])
        lst = tmpd / "list.txt"
        lst.write_text("".join(f"file '{f}'\nduration {n / fps:.5f}\n" for f, n in runs)
                       + f"file '{runs[-1][0]}'\n")
        dur = total / fps
        cmd = ["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(lst)]
        if fl.get("music"):
            cmd += ["-stream_loop", "-1", "-i", str(ROOT / fl["music"])]
        else:
            cmd += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100"]
        cmd += ["-filter_complex",
                f"[0:v]fps={fps},format=yuv420p[v];[1:a]atrim=0:{dur:.3f},asetpts=PTS-STARTPTS,"
                f"afade=t=out:st={max(dur - 1.0, 0):.3f}:d=1.0[a]",
                "-map", "[v]", "-map", "[a]", "-t", f"{dur:.3f}", "-c:v", "libx264",
                "-preset", "veryfast", "-crf", "20", "-r", str(fps), "-c:a", "aac", "-b:a", "128k",
                "-movflags", "+faststart", str(out)]
        out.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(cmd, check=True)
    print(f"  secret frame(s) at {s_at / fps:.2f}s for {s_len} frame(s)")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only")
    ap.add_argument("--skip-existing", action="store_true")
    args = ap.parse_args()
    _ = font(10)  # fail early if fonts are missing
    made = build(args.only, args.skip_existing)
    print(f"\nBuilt {len(made)} file(s). Previews in media/previews/")


if __name__ == "__main__":
    main()
