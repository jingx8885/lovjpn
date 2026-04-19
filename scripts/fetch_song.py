#!/usr/bin/env python3
"""Fetch a song from tonzhon.com: LRC lyrics + MP3 audio. Emit a skeleton JSON.

Usage:
  fetch_song.py "<query>" <output_dir> [--prefer-netease] [--prefer-youtube] [--no-audio]

The output is <output_dir>/skeleton.json — a sentence list with timestamps but empty
`words / translation / note`. Claude fills those in, then runs build_study.py.
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path


TONZHON_API = "https://tonzhon.com/api.php"
NETEASE_OUTER = "https://music.163.com/song/media/outer/url?id={id}.mp3"


def post_form(url: str, fields: dict) -> bytes:
    """POST as application/x-www-form-urlencoded. tonzhon.com rejects the
    default urllib User-Agent (TLS handshake gets dropped), so we spoof a
    browser UA to match what the site's own frontend sends."""
    body = urllib.parse.urlencode(fields).encode("utf-8")
    req = urllib.request.Request(
        url, data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
            "Referer": "https://tonzhon.com/",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.read()


def search_tonzhon(query: str, count: int = 10, source: str = "netease") -> list:
    raw = post_form(TONZHON_API, {
        "types": "search", "count": count, "source": source, "name": query,
    })
    return json.loads(raw.decode("utf-8"))


def fetch_lyric(song_id: str, source: str = "netease") -> str:
    raw = post_form(TONZHON_API, {
        "types": "lyric", "id": song_id, "source": source,
    })
    obj = json.loads(raw.decode("utf-8"))
    return obj.get("lyric", "")


LRC_LINE = re.compile(r"^\[(\d+):(\d+(?:\.\d+)?)\](.*)$")


def parse_lrc(lrc: str) -> list:
    """Return [{'ms': int, 'text': str}, ...] sorted by time, skipping meta lines."""
    out = []
    meta_prefix = ("作词", "作曲", "编曲", "作詞", "作曲", "編曲", "Lyrics",
                   "lyrics by", "composer", "arranger")
    for line in lrc.splitlines():
        m = LRC_LINE.match(line.strip())
        if not m:
            continue
        mins, secs, text = m.group(1), m.group(2), m.group(3).strip()
        if not text:
            continue
        if any(text.startswith(p) or (":" in text[:8] and text.startswith(p))
               for p in meta_prefix):
            continue
        # Strip trailing meta/annotations like "作词 : X"
        if re.match(r"^(作词|作曲|编曲|作詞|編曲)\s*[:：]", text):
            continue
        ms = int((int(mins) * 60 + float(secs)) * 1000)
        out.append({"ms": ms, "text": text})
    out.sort(key=lambda x: x["ms"])
    # de-duplicate identical consecutive lines
    dedup = []
    for item in out:
        if dedup and dedup[-1]["text"] == item["text"] and item["ms"] - dedup[-1]["ms"] < 300:
            continue
        dedup.append(item)
    return dedup


def probe_netease_url(song_id: str) -> bool:
    """HEAD-style probe: does NetEase's outer URL actually serve audio for this id?

    NetEase answers `outer/url?id=X.mp3` with a 302 — either to a CDN MP3 or to
    `/404`. Geo/version-restricted tracks always hit /404, so iterating the
    tonzhon search results and picking the first id whose redirect target is NOT
    /404 lets us skip the blocked originals and land on a playable alternate
    (often a Japanese cover whose lyrics align with the LRC).
    """
    url = NETEASE_OUTER.format(id=song_id)
    cmd = ["curl", "-sI", "-o", "/dev/null", "-w",
           "%{http_code}\n%{redirect_url}\n", "--max-redirs", "0", url]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    except subprocess.TimeoutExpired:
        return False
    info = (r.stdout or "").strip().splitlines()
    status = info[0] if info else ""
    redirect = info[1] if len(info) > 1 else ""
    return status.startswith("3") and "/404" not in redirect


def try_netease_download(song_id: str, out_path: Path) -> bool:
    """Download MP3 via NetEase outer URL. Returns True on success, False if blocked."""
    url = NETEASE_OUTER.format(id=song_id)
    cmd = ["curl", "-sL", "-o", str(out_path), "-w", "%{http_code}\n%{url_effective}\n", url]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        return False
    info = r.stdout.strip().splitlines()
    status = info[0] if info else ""
    final_url = info[1] if len(info) > 1 else ""
    if status.startswith("2") and "/404" not in final_url and out_path.exists() and out_path.stat().st_size > 50_000:
        return True
    if out_path.exists():
        out_path.unlink()
    return False


def pick_playable_netease(results: list, prefer_artist: str | None = None) -> int | None:
    """Scan tonzhon search hits and return the index of the first one whose
    NetEase outer URL actually resolves to audio AND that has non-trivial LRC.
    Some re-uploads on NetEase have playable audio but an empty lyric field
    (just a title or a couple metadata lines); those make for a useless study
    page since the sentence list would be empty. We require both.
    """
    def _score(title: str) -> int:
        low = (title or "").lower()
        # Lower score = preferred. Keep the original intent here narrow: we
        # only demote things that are clearly *not* the standard recording.
        bad = ("remix", "ballade", "tv size", "off vocal", "instrumental",
               "karaoke", "カラオケ", "piano", "acoustic",
               # Live versions play but their timing drifts from the studio LRC.
               "(live)", " live", "ライブ", "ライヴ",
               # Covers on NetEase aren't the original vocals.
               "cover", "カバー",
               # Transposed karaoke uploads occasionally slip through.
               "+1key", "+2key", "-1key", "-2key")
        return sum(1 for b in bad if b in low)

    # Constrain the search to actual matches of the top-ranked title. When a
    # song is geo-blocked, tonzhon still returns 50+ unrelated tracks further
    # down the list (other tracks by the same artist, random covers, etc.);
    # falling through blindly means we'd ship a completely different song as
    # "Get along". Require the canonical title word(s) of the top result to
    # appear in the candidate's name.
    def _norm(s: str) -> str:
        return re.sub(r"[\s\(\)\[\]【】、，・~〜\-\—]+", "", (s or "").lower())
    canon = _norm(results[0].get("name", "")) if results else ""
    eligible = list(range(len(results)))
    if canon:
        eligible = [i for i in eligible
                    if canon in _norm(results[i].get("name", ""))
                    or _norm(results[i].get("name", "")) in canon]

    ranked = sorted(eligible, key=lambda i: _score(results[i].get("name", "")))
    # First pass: pick the top-ranked candidate that has BOTH playable audio
    # and a real LRC (> ~80 chars rules out empty/meta-only tracks).
    fallback_audio_only = None
    for i in ranked:
        song_id = results[i].get("id")
        if not song_id:
            continue
        if not probe_netease_url(str(song_id)):
            continue
        try:
            lrc = fetch_lyric(str(song_id))
        except Exception:
            lrc = ""
        timestamped = [ln for ln in lrc.splitlines()
                       if LRC_LINE.match(ln.strip())]
        if len(timestamped) >= 8:
            return i
        if fallback_audio_only is None:
            fallback_audio_only = i
    # Nothing had both audio + LRC. Rather than ship silence, fall back to the
    # best audio-only candidate — the caller will still get a usable MP3, just
    # without timestamps.
    return fallback_audio_only


# Keep this tight: only reject variants that are clearly NOT the track the
# listener wants. "edit"/"edited" stays OUT — many full songs on SoundCloud
# have titles ending in "edit" just because the uploader trimmed silence.
BAD_TITLE_TOKENS = (
    "remix", "cover", "flip", "nightcore", "slowed", "reverb",
    "sped up", "8d", "karaoke", "カラオケ", "instrumental", "off vocal",
    "オフボーカル", "伴奏", "inst.", "inst ", "piano ver", "acoustic ver",
    "live at", "first take", "ザ・ファースト", "歌ってみた",
    # Japanese karaoke channels mark originals this way; the audio itself
    # is always an instrumental backing track.
    "原曲歌手", "歌っちゃ王",
    # +NKey / -NKey in the title = transposed karaoke.
    "+1key", "+2key", "+3key", "+4key", "+5key",
    "-1key", "-2key", "-3key", "-4key", "-5key",
)


def _is_clean_title(title: str) -> bool:
    low = (title or "").lower()
    return not any(tok in low for tok in BAD_TITLE_TOKENS)


def _list_candidates(query: str, ytdlp: str, provider: str, count: int) -> list:
    """Return [{url, duration, title}] from yt-dlp flat search."""
    prefix = {"soundcloud": f"scsearch{count}:", "youtube": f"ytsearch{count}:"}[provider]
    cmd = [
        ytdlp, f"{prefix}{query}", "--no-playlist", "--flat-playlist",
        "--print", "%(webpage_url)s\t%(duration)s\t%(title)s",
        "--quiet", "--no-warnings",
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        return []
    out = []
    for ln in (r.stdout or "").strip().splitlines():
        parts = ln.split("\t", 2)
        if len(parts) != 3:
            continue
        url, dur, title = parts
        try:
            d = float(dur) if dur and dur != "NA" else None
        except ValueError:
            d = None
        out.append({"url": url, "duration": d, "title": title})
    return out


def _pick_best_match(cands: list, target_s: float | None, tol: float = 8.0) -> dict | None:
    """Pick the first search result whose title isn't obviously wrong.

    Trust the platform's ranking: the most-played / most-linked upload is
    usually the real thing. Duration-based scoring was tempting but it bit us
    — it picked instrumental or karaoke versions whose runtime happened to
    match. Instead, just filter out known-bad titles and take the first
    remaining candidate. If EVERY candidate is bad (e.g. the only uploads are
    a karaoke channel's backing tracks), return None so the caller can fall
    back to the next provider / audio=none rather than ship a page with a
    vocals-free "original".
    """
    if not cands:
        return None
    for c in cands:
        if _is_clean_title(c["title"]):
            return c
    return None


def try_ytdlp_search(query: str, out_path: Path, ytdlp: str, provider: str,
                     target_duration_s: float | None = None) -> tuple[bool, str]:
    """Download audio via yt-dlp, picking the candidate closest to target_duration_s.

    Returns (success, chosen_title). The old one-shot scsearch1/ytsearch1 flow
    often returned "edited" / "live" / "cover" versions whose runtime doesn't
    match the LRC — see evaluator's complaint that 群青 lyrics scroll past the
    audio. We now list ~15 candidates, filter out obviously bad titles, and
    pick by duration proximity.
    """
    cands = _list_candidates(query, ytdlp, provider, count=15)
    if not cands:
        return (False, "")
    best = _pick_best_match(cands, target_duration_s)
    if best is None:
        return (False, "")
    dur_note = (f"{best['duration']:.1f}s" if best["duration"] else "duration unknown")
    print(f"  {provider}: picked \"{best['title']}\" ({dur_note}) — {best['url']}",
          file=sys.stderr)

    tmp_tpl = str(out_path.with_suffix(".%(ext)s"))
    cmd = [
        ytdlp,
        "-x", "--audio-format", "mp3",
        "--audio-quality", "0",
        "-o", tmp_tpl,
        "--no-playlist",
        "--quiet", "--no-warnings",
        "--force-overwrites",
        best["url"],
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        return (False, best["title"])
    if r.returncode != 0:
        msg = (r.stderr or "").strip().splitlines()
        if msg:
            print(f"  {provider} failed: {msg[-1][:200]}", file=sys.stderr)
        return (False, best["title"])
    if out_path.exists() and out_path.stat().st_size > 50_000:
        return (True, best["title"])
    for ext in (".m4a", ".opus", ".webm"):
        alt = out_path.with_suffix(ext)
        if alt.exists():
            alt.rename(out_path)
            return (True, best["title"])
    return (False, best["title"])


def build_skeleton(song: dict, lines: list, audio_file: str | None,
                   audio_source: str, tonzhon_url: str | None) -> dict:
    title = f"{song['name']} · {'/'.join([a for arr in song['artist'] for a in arr])}"
    sentences = []
    for i, line in enumerate(lines, 1):
        sentences.append({
            "id": f"s{i}",
            "timestamp_ms": line["ms"],
            "raw": line["text"],  # Claude uses this to fill `words`
            "words": [],          # TO FILL
            "translation": "",    # TO FILL
            "note": "",           # TO FILL (optional)
        })
    return {
        "title": title,
        "source": {
            "provider": "tonzhon.com / netease",
            "netease_id": song["id"],
            "tonzhon_url": tonzhon_url,
        },
        "audio": {
            "file": audio_file,            # relative to output dir, or null
            "source": audio_source,        # "netease" | "youtube" | "none"
        },
        "sentences": sentences,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("query", help='Song query, e.g. "YOASOBI 群青"')
    ap.add_argument("output_dir", help="Output directory (created if missing)")
    ap.add_argument("--index", type=int, default=0,
                    help="Which search result to use (0 = top)")
    ap.add_argument("--no-audio", action="store_true", help="Skip MP3 download")
    ap.add_argument("--prefer-ytdlp", action="store_true",
                    help="Skip NetEase outer URL, go straight to yt-dlp")
    ap.add_argument("--ytdlp-provider", choices=["soundcloud", "youtube", "auto"],
                    default="auto",
                    help="Which yt-dlp source to try first (auto = soundcloud then youtube)")
    args = ap.parse_args()

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Searching tonzhon.com for: {args.query}", file=sys.stderr)
    results = search_tonzhon(args.query)
    if not results:
        print("ERROR: no search results", file=sys.stderr)
        sys.exit(1)

    if args.index >= len(results):
        print(f"ERROR: index {args.index} out of range (got {len(results)} results)",
              file=sys.stderr)
        sys.exit(1)

    # If the user pinned an index, honor it. Otherwise auto-pick the first
    # result whose NetEase URL is actually playable — many top-ranked hits on
    # tonzhon are geo-restricted originals that 404, while a Japanese cover a
    # few rows down streams fine and has the same LRC timing.
    chosen_index = args.index
    if args.index == 0 and not args.no_audio and not args.prefer_ytdlp:
        print("Probing NetEase outer URLs across search results...", file=sys.stderr)
        playable = pick_playable_netease(results)
        if playable is not None and playable != 0:
            print(f"  result #0 is geo-blocked; falling through to #{playable}",
                  file=sys.stderr)
            chosen_index = playable
        elif playable is None:
            print("  no search result has a playable NetEase URL — "
                  "will fall back to yt-dlp after LRC fetch.", file=sys.stderr)

    song = results[chosen_index]
    artists = " / ".join(a for arr in song["artist"] for a in arr)
    print(f"Picked #{chosen_index}: {song['name']} — {artists} (id {song['id']})",
          file=sys.stderr)

    print("Fetching lyrics...", file=sys.stderr)
    lrc = fetch_lyric(song["id"])
    if not lrc.strip():
        print("ERROR: empty lyrics", file=sys.stderr)
        sys.exit(1)
    (out_dir / "lyrics.lrc").write_text(lrc, encoding="utf-8")

    lines = parse_lrc(lrc)
    print(f"Parsed {len(lines)} lyric lines", file=sys.stderr)

    # Target duration for picking a matching audio source: last lyric timestamp
    # (in seconds) plus ~8s for a typical outro. Songs whose fetched audio is
    # much shorter/longer than this are almost certainly edits/covers/live cuts
    # — the reason lyrics appear to slide off the music.
    target_dur_s = (lines[-1]["ms"] / 1000 + 8) if lines else None
    if target_dur_s:
        print(f"  LRC-derived target duration: ~{target_dur_s:.0f}s", file=sys.stderr)

    audio_file = None
    audio_source = "none"
    if not args.no_audio:
        mp3_path = out_dir / "original.mp3"
        if not args.prefer_ytdlp:
            print("Trying NetEase outer URL...", file=sys.stderr)
            if try_netease_download(song["id"], mp3_path):
                audio_file = "original.mp3"
                audio_source = "netease"
                print(f"  ✅ got {mp3_path.stat().st_size // 1024} KB", file=sys.stderr)
            else:
                print("  NetEase blocked (version-restricted); trying yt-dlp...",
                      file=sys.stderr)

        if audio_file is None:
            ytdlp_path = (shutil.which("yt-dlp")
                          or str(Path(sys.executable).parent / "yt-dlp"))
            if not Path(ytdlp_path).exists():
                print("  ❌ yt-dlp not found — skeleton will have no audio",
                      file=sys.stderr)
            else:
                providers = ({"soundcloud": ["soundcloud"],
                              "youtube": ["youtube"],
                              "auto": ["soundcloud", "youtube"]})[args.ytdlp_provider]
                for prov in providers:
                    print(f"  Trying yt-dlp/{prov}...", file=sys.stderr)
                    ok, _ = try_ytdlp_search(args.query, mp3_path, ytdlp_path,
                                             prov, target_duration_s=target_dur_s)
                    if ok:
                        audio_file = "original.mp3"
                        audio_source = prov
                        # Warn the user when we grabbed something that clearly
                        # doesn't match the LRC runtime — they may want to drop
                        # in a local MP3 instead.
                        if target_dur_s is not None:
                            actual = 0.0
                            try:
                                probe = subprocess.run(
                                    ["ffprobe", "-v", "quiet",
                                     "-show_entries", "format=duration",
                                     "-of", "default=noprint_wrappers=1:nokey=1",
                                     str(mp3_path)],
                                    capture_output=True, text=True, timeout=15,
                                )
                                actual = float((probe.stdout or "0").strip() or 0)
                            except (subprocess.TimeoutExpired, ValueError, FileNotFoundError):
                                pass
                            if actual and abs(actual - target_dur_s) > 8:
                                print(
                                    f"  ⚠️  fetched audio is {actual:.0f}s but LRC "
                                    f"expects ~{target_dur_s:.0f}s "
                                    f"(Δ {actual - target_dur_s:+.0f}s). Lyrics will "
                                    f"drift — consider dropping a local MP3 in as "
                                    f"original.mp3 and setting audio.source=local.",
                                    file=sys.stderr,
                                )
                        print(f"  ✅ got {mp3_path.stat().st_size // 1024} KB via {prov}",
                              file=sys.stderr)
                        break
                if audio_file is None:
                    print("  ❌ all audio sources failed — skeleton will have no audio",
                          file=sys.stderr)

    tonzhon_url = (
        "https://tonzhon.com/?wd="
        + urllib.parse.quote(args.query)
        + "&source=netease"
    )
    skeleton = build_skeleton(song, lines, audio_file, audio_source, tonzhon_url)
    (out_dir / "skeleton.json").write_text(
        json.dumps(skeleton, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\nSkeleton written: {out_dir / 'skeleton.json'}", file=sys.stderr)
    print(f"  {len(lines)} sentences to fill. Audio: {audio_source}", file=sys.stderr)
    print(str(out_dir / "skeleton.json"))


if __name__ == "__main__":
    main()
