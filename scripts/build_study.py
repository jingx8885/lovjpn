#!/usr/bin/env python3
"""Build a Japanese study HTML page from a filled JSON spec.

The JSON may carry an optional `audio` block (original song MP3 alongside the
HTML) and per-sentence `timestamp_ms` values. When present, the page shows a
top player that auto-highlights the current sentence and exposes a "🎵 跳到原曲"
button per sentence. Per-sentence TTS (edge-tts) is always available.
"""

import argparse
import asyncio
import html as htmllib
import json
import sys
from pathlib import Path

try:
    import edge_tts
except ImportError:
    print(
        "ERROR: edge-tts not installed. Use the skill venv: "
        "<skill>/.venv/bin/python3 build_study.py ...",
        file=sys.stderr,
    )
    sys.exit(1)


DEFAULT_VOICE = "ja-JP-NanamiNeural"


def esc(s):
    return htmllib.escape(str(s or ""), quote=True)


def ms_to_mmss(ms: int) -> str:
    s = max(0, int(ms) // 1000)
    return f"{s // 60}:{s % 60:02d}"


async def _synth_one(text: str, out_path: Path, voice: str):
    tts = edge_tts.Communicate(text, voice)
    await tts.save(str(out_path))


def _tts_text(sentence) -> str:
    """Reconstruct the speakable text from words. Skip if there's no real kana/kanji."""
    text = "".join(w.get("text", "") for w in sentence.get("words", [])).strip()
    if not text:
        return ""
    if not any("\u3040" <= c <= "\u30ff" or "\u4e00" <= c <= "\u9fff" for c in text):
        return ""
    return text


async def synth_all(sentences, out_dir: Path, voice: str, overwrite: bool,
                    concurrency: int = 8):
    """Synthesize TTS in capped-concurrency batches (edge-tts rate-limits at high fan-out)."""
    jobs = []
    for s in sentences:
        text = _tts_text(s)
        if not text:
            continue
        target = out_dir / f"{s['id']}.mp3"
        if target.exists() and not overwrite:
            continue
        jobs.append((text, target))
    if not jobs:
        return
    sem = asyncio.Semaphore(concurrency)

    async def _bounded(text, target):
        async with sem:
            try:
                await _synth_one(text, target, voice)
            except Exception as e:
                print(f"  TTS failed for {target.name}: {e}", file=sys.stderr)

    await asyncio.gather(*(_bounded(t, p) for t, p in jobs))


def build_table(sentence: dict) -> str:
    words = sentence.get("words", [])
    if not words:
        raw = sentence.get("raw", "")
        return f'<table><tr><th>原文</th><td>{esc(raw)}</td></tr><tr class="translation"><th>翻译</th><td><em>（未拆解）</em></td></tr></table>'

    n = len(words)

    def row(label, key):
        cells = "".join(f"<td>{esc(w.get(key))}</td>" for w in words)
        return f"<tr><th>{label}</th>{cells}</tr>"

    rows = [
        row("原文", "text"),
        row("假名", "kana"),
        row("罗马音", "romaji"),
        row("拆解", "meaning"),
        f'<tr class="translation"><th>翻译</th>'
        f'<td colspan="{n}">{esc(sentence.get("translation", ""))}</td></tr>',
    ]
    return "<table>" + "".join(rows) + "</table>"


def build_sentence_block(s: dict, has_original: bool) -> str:
    sid = esc(s["id"])
    ts_ms = s.get("timestamp_ms")
    data_ms = f' data-ms="{int(ts_ms)}"' if ts_ms is not None else ""
    table = build_table(s)
    note = (s.get("note") or "").strip()
    note_html = (
        f'<div class="note"><strong>💡 日本人思路：</strong>{esc(note)}</div>'
        if note else ""
    )

    actions = []
    if has_original and ts_ms is not None:
        actions.append(
            f'<button class="btn-original" data-ms="{int(ts_ms)}" '
            f'title="跳到原曲 {ms_to_mmss(ts_ms)} 并播放">'
            f'🎵 原曲 {ms_to_mmss(ts_ms)}</button>'
        )
        actions.append(
            f'<button class="btn-loop" data-id="{sid}" '
            f'title="单句循环：在该句的时间区间里反复播放原曲，再点一次取消">'
            f'🔁 循环</button>'
        )
    actions.append(
        f'<button class="btn-tts" data-id="{sid}" title="edge-tts 日语朗读（慢）">'
        f'▶ 跟读</button>'
    )
    actions.append(
        f'<audio id="tts-{sid}" src="{sid}.mp3" preload="none"></audio>'
    )
    actions_html = f'<div class="actions">{"".join(actions)}</div>'

    return (
        f'<section class="sentence" id="sentence-{sid}"{data_ms}>'
        f"{actions_html}{table}{note_html}</section>"
    )


_PRONOUN_CSV = Path(__file__).resolve().parent.parent / "references" / "pronoun.md"


def build_kana_origin_panel() -> str:
    """Render references/pronoun.md (a CSV of 五十音字源 + 中古音 + 演变说明) as a
    collapsible reference table. Static — shown verbatim on every page."""
    if not _PRONOUN_CSV.exists():
        return ""
    lines = [ln for ln in _PRONOUN_CSV.read_text(encoding="utf-8").splitlines() if ln.strip()]
    if not lines:
        return ""
    head, *body = lines
    headers = [h.strip() for h in head.split(",")]
    thead = "<tr>" + "".join(f"<th>{esc(h)}</th>" for h in headers) + "</tr>"
    rows = []
    for ln in body:
        # Simple CSV: commas are safe here because the source never contains them in-cell.
        cells = [c.strip() for c in ln.split(",")]
        # cells may contain <sup>…</sup>; keep that HTML as-is.
        rows.append("<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>")
    return (
        '<details class="kana-origin">'
        '<summary>📖 五十音字源（平假名+片假名 · 汉字草书/偏旁 · 中古拟音 · 古中日演变）</summary>'
        f'<table class="ko-table">{thead}{"".join(rows)}</table>'
        '</details>'
    )


def build_html(data: dict) -> str:
    title = esc(data.get("title", "Japanese Study"))
    audio = data.get("audio") or {}
    audio_file = audio.get("file")
    audio_source = audio.get("source", "none")
    has_original = bool(audio_file) and audio_source != "none"
    playback = data.get("playback") or {}
    raw_presets = playback.get("speed_presets", [1.0, 0.85, 0.7])
    presets = []
    for x in raw_presets if isinstance(raw_presets, list) else []:
        try:
            r = float(x)
        except (TypeError, ValueError):
            continue
        if 0.5 <= r <= 1.5 and r not in presets:
            presets.append(r)
    if not presets:
        presets = [1.0, 0.85, 0.7]
    try:
        default_rate = float(playback.get("default_speed", 1.0))
    except (TypeError, ValueError):
        default_rate = 1.0
    if default_rate not in presets:
        default_rate = min(presets, key=lambda r: abs(r - default_rate))

    def _speed_label(rate: float) -> str:
        return f"{rate:.1f}x" if abs(rate - round(rate)) < 1e-8 else f"{rate:g}x"

    speed_controls = (
        '<div class="speed-controls" aria-label="播放速度">'
        '<span class="speed-label">速度</span>'
        + "".join(
            f'<button type="button" class="btn-speed" data-rate="{r:g}">{_speed_label(r)}</button>'
            for r in presets
        )
        + '</div>'
    )
    speed_storage_key = f"lovjpn:playback-rate:{data.get('title','')}::{audio_file or 'no-audio'}"

    if has_original:
        source_label = {
            "netease": "网易云外链",
            "soundcloud": "SoundCloud",
            "youtube": "YouTube",
            "local": "本地文件",
        }.get(audio_source, audio_source)
        player = (
            f'<div class="main-player">'
            f'<audio id="main-audio" src="{esc(audio_file)}" controls preload="metadata"></audio>'
            f'<span class="source-tag">原曲来源：{esc(source_label)}</span>'
            f'{speed_controls}'
            f"</div>"
        )
    else:
        tonzhon = (data.get("source") or {}).get("tonzhon_url")
        if tonzhon:
            player = (
                f'<div class="main-player-fallback">'
                f'原曲未下载。可在 <a href="{esc(tonzhon)}" target="_blank">tonzhon.com</a> 在线试听，'
                f"或把 MP3 文件重命名为 <code>original.mp3</code> 放在本目录。"
                f"</div>"
            )
        else:
            player = (
                '<div class="main-player-fallback">'
                "原曲未下载。把 MP3 重命名为 <code>original.mp3</code> 放在本目录即可启用同步。"
                "</div>"
            )

    sentences = data.get("sentences", [])
    kana_panel = build_kana_origin_panel()
    blocks = "\n".join(build_sentence_block(s, has_original) for s in sentences)

    return f"""<!DOCTYPE html>
<html lang="zh-Hans">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  :root {{
    --accent: #3a7d5c;
    --accent-dark: #2b5a43;
    --warm: #d88a2b;
    --warm-bg: #fff8e6;
    --paper: #fafaf7;
    --line: #d6d6d0;
  }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Hiragino Sans",
                 "Noto Sans CJK SC", "Microsoft YaHei", sans-serif;
    max-width: 1100px; margin: 0 auto; padding: 1em 1.2em 6em;
    color: #1f1f1f; line-height: 1.65; background: var(--paper);
  }}
  header.top {{
    position: sticky; top: 0; z-index: 10;
    background: rgba(250, 250, 247, 0.96);
    backdrop-filter: blur(6px);
    padding: .8em 0 .7em;
    border-bottom: 1px solid var(--line);
    margin-bottom: 1em;
  }}
  h1 {{ margin: 0 0 .4em; color: var(--accent-dark); font-size: 1.4em; }}
  .main-player {{ display: flex; align-items: center; gap: .8em; flex-wrap: wrap; }}
  .main-player audio {{ flex: 1; min-width: 280px; max-width: 100%; }}
  .speed-controls {{
    display: inline-flex; align-items: center; gap: .4em; flex-wrap: wrap;
    margin-left: auto;
  }}
  .speed-label {{
    font-size: .82em; color: #666;
    background: #eef4ef; padding: .18em .55em; border-radius: 10px;
  }}
  .btn-speed {{
    border: 1px solid #c9d9cc; background: #fff; color: #2b5a43;
    border-radius: 14px; padding: .2em .7em; cursor: pointer;
    font-size: .86em; line-height: 1.2;
  }}
  .btn-speed.active {{
    background: var(--accent); color: #fff; border-color: var(--accent);
  }}
  .source-tag {{
    font-size: .82em; color: #666; background: #eef4ef;
    padding: .2em .6em; border-radius: 10px;
  }}
  .main-player-fallback {{
    padding: .7em 1em; background: #fdf2e0; border-radius: 6px;
    font-size: .92em; color: #7a4a00;
  }}
  .main-player-fallback a {{ color: var(--accent-dark); }}
  .main-player-fallback code {{
    background: #fff; padding: .1em .4em; border-radius: 3px;
    border: 1px solid var(--line); font-size: .9em;
  }}
  section.sentence {{
    margin: 1em 0; padding: .9em 1em; background: #fff;
    border-radius: 10px; border-left: 4px solid var(--accent);
    box-shadow: 0 1px 3px rgba(0,0,0,.04);
    transition: background .25s, border-color .25s, transform .25s;
  }}
  section.sentence.active {{
    background: var(--warm-bg);
    border-left-color: var(--warm);
    transform: scale(1.005);
    box-shadow: 0 2px 8px rgba(216, 138, 43, .2);
  }}
  .actions {{ display: flex; gap: .5em; flex-wrap: wrap; margin-bottom: .6em; }}
  .actions button {{
    border: 0; border-radius: 16px; padding: .32em 1em;
    cursor: pointer; font-size: .9em; color: #fff;
    transition: background .15s, transform .1s;
  }}
  .actions button:active {{ transform: translateY(1px); }}
  .btn-original {{ background: var(--accent); }}
  .btn-original:hover {{ background: var(--accent-dark); }}
  .btn-loop {{ background: #6a5cb8; }}
  .btn-loop:hover {{ background: #524597; }}
  .btn-loop.active {{
    background: var(--warm); color: #fff;
    box-shadow: 0 0 0 2px rgba(216, 138, 43, .35);
  }}
  .btn-tts {{ background: #5864a3; }}
  .btn-tts:hover {{ background: #424d82; }}
  .btn-tts.playing {{ background: var(--warm); }}
  section.sentence.looping {{
    border-left-color: var(--warm);
    background: var(--warm-bg);
  }}
  table {{
    border-collapse: collapse; margin: .2em 0; width: 100%;
    font-size: .98em; table-layout: auto;
  }}
  th, td {{
    border: 1px solid var(--line); padding: .42em .55em;
    text-align: center; vertical-align: middle;
  }}
  th {{
    background: #eef4ef; font-weight: 600; color: var(--accent-dark);
    white-space: nowrap; width: 64px;
  }}
  td {{ background: #fff; min-width: 40px; }}
  tr.translation td {{
    text-align: left; padding: .6em .9em;
    background: var(--warm-bg); font-weight: 500; font-size: 1.02em;
  }}
  .note {{
    margin-top: .6em; padding: .55em .85em; background: #fdf2e0;
    border-left: 3px solid var(--warm); border-radius: 4px; font-size: .94em;
  }}
  details.kana-origin {{
    margin-top: .6em; padding: .3em .7em;
    background: #eef4ef; border-radius: 6px;
    font-size: .9em;
  }}
  body.kana-open header.top {{
    position: static;
  }}
  details.kana-origin[open] {{
    max-height: 62vh;
    overflow: auto;
    overscroll-behavior: contain;
  }}
  details.kana-origin[open] .ko-table {{
    display: block;
    max-height: calc(62vh - 2.2em);
    overflow: auto;
  }}
  details.kana-origin summary {{
    cursor: pointer; font-weight: 600; color: var(--accent-dark);
    padding: .2em 0;
  }}
  details.kana-origin summary:hover {{ color: var(--accent); }}
  table.ko-table {{ margin-top: .5em; font-size: .88em; width: 100%; }}
  table.ko-table th, table.ko-table td {{
    padding: .3em .5em; text-align: left; vertical-align: top;
  }}
  table.ko-table td:first-child {{
    color: #666; white-space: nowrap; width: 40px;
  }}
  @media (max-width: 600px) {{
    body {{ padding: .5em .7em 5em; }}
    th {{ width: auto; }}
  }}
</style>
</head>
<body>
<header class="top">
  <h1>{title}</h1>
  {player}
  {kana_panel}
</header>
{blocks}
<script>
(function() {{
  const mainAudio = document.getElementById('main-audio');
  const speedButtons = Array.from(document.querySelectorAll('.btn-speed'));
  const ttsAudios = Array.from(document.querySelectorAll('audio[id^="tts-"]'));
  const speedPresets = {json.dumps(presets)};
  const defaultRate = {default_rate};
  const speedStorageKey = {json.dumps(speed_storage_key)};
  const kanaDetails = document.querySelector('details.kana-origin');
  const sentences = Array.from(document.querySelectorAll('section.sentence[data-ms]'));
  const timedSentences = sentences
    .map(s => ({{el: s, ms: parseInt(s.dataset.ms, 10)}}))
    .filter(x => !isNaN(x.ms))
    .sort((a, b) => a.ms - b.ms);

  function normalizeRate(x) {{
    const v = Number(x);
    if (!Number.isFinite(v)) return defaultRate;
    return speedPresets.includes(v) ? v : defaultRate;
  }}

  function applyRate(rate, persist = true) {{
    const next = normalizeRate(rate);
    if (mainAudio) mainAudio.playbackRate = next;
    ttsAudios.forEach(a => {{ a.playbackRate = next; }});
    speedButtons.forEach(btn => {{
      const r = Number(btn.dataset.rate || defaultRate);
      btn.classList.toggle('active', Math.abs(r - next) < 1e-8);
    }});
    if (persist) {{
      try {{ localStorage.setItem(speedStorageKey, String(next)); }} catch (e) {{}}
    }}
    return next;
  }}

  let currentRate = defaultRate;
  try {{
    const stored = localStorage.getItem(speedStorageKey);
    if (stored !== null) currentRate = normalizeRate(stored);
  }} catch (e) {{}}
  currentRate = applyRate(currentRate, false);

  speedButtons.forEach(btn => {{
    btn.addEventListener('click', () => {{
      currentRate = applyRate(btn.dataset.rate, true);
    }});
  }});

  function syncKanaOpenState() {{
    document.body.classList.toggle('kana-open', !!(kanaDetails && kanaDetails.open));
  }}
  if (kanaDetails) {{
    kanaDetails.addEventListener('toggle', syncKanaOpenState);
    syncKanaOpenState();
  }}

  function setActive(el) {{
    document.querySelectorAll('section.sentence.active').forEach(s => {{
      if (s !== el) s.classList.remove('active');
    }});
    if (el && !el.classList.contains('active')) el.classList.add('active');
  }}

  let userScrolledRecently = 0;
  window.addEventListener('scroll', () => {{ userScrolledRecently = Date.now(); }});

  function isInViewport(el) {{
    const r = el.getBoundingClientRect();
    return r.top >= 0 && r.bottom <= (window.innerHeight || document.documentElement.clientHeight);
  }}

  let loopState = null;  // {{id, startMs, endMs, btn, section}}

  function cancelLoop() {{
    if (loopState) {{
      if (loopState.btn) loopState.btn.classList.remove('active');
      if (loopState.section) loopState.section.classList.remove('looping');
      loopState = null;
    }}
  }}

  function endMsFor(section) {{
    const idx = timedSentences.findIndex(x => x.el === section);
    if (idx >= 0 && idx + 1 < timedSentences.length) return timedSentences[idx + 1].ms;
    if (mainAudio && !isNaN(mainAudio.duration)) return mainAudio.duration * 1000;
    return Infinity;
  }}

  if (mainAudio && timedSentences.length) {{
    mainAudio.addEventListener('timeupdate', () => {{
      const ms = mainAudio.currentTime * 1000;

      if (loopState) {{
        if (ms + 40 >= loopState.endMs || ms + 200 < loopState.startMs) {{
          mainAudio.currentTime = loopState.startMs / 1000;
          return;
        }}
      }}

      let active = null;
      for (const {{el, ms: t}} of timedSentences) {{
        if (t <= ms + 50) active = el; else break;
      }}
      setActive(active);
      if (active && !mainAudio.paused
          && !document.body.classList.contains('kana-open')
          && Date.now() - userScrolledRecently > 2500
          && !isInViewport(active)) {{
        active.scrollIntoView({{behavior: 'smooth', block: 'center'}});
      }}
    }});
  }}

  document.querySelectorAll('.btn-original').forEach(btn => {{
    btn.addEventListener('click', () => {{
      if (!mainAudio) return;
      cancelLoop();
      const ms = parseInt(btn.dataset.ms, 10) || 0;
      mainAudio.currentTime = ms / 1000;
      mainAudio.play();
    }});
  }});

  document.querySelectorAll('.btn-loop').forEach(btn => {{
    btn.addEventListener('click', () => {{
      if (!mainAudio) return;
      const id = btn.dataset.id;
      if (loopState && loopState.id === id) {{
        cancelLoop();
        return;
      }}
      cancelLoop();
      const section = document.getElementById('sentence-' + id);
      if (!section) return;
      const startMs = parseInt(section.dataset.ms, 10);
      const endMs = endMsFor(section);
      loopState = {{id, startMs, endMs, btn, section}};
      btn.classList.add('active');
      section.classList.add('looping');
      mainAudio.currentTime = startMs / 1000;
      mainAudio.play();
    }});
  }});

  document.querySelectorAll('.btn-tts').forEach(btn => {{
    btn.addEventListener('click', () => {{
      document.querySelectorAll('audio').forEach(a => {{
        if (a.id && a.id.startsWith('tts-')) {{ a.pause(); a.currentTime = 0; }}
      }});
      document.querySelectorAll('.btn-tts.playing').forEach(b => b.classList.remove('playing'));
      if (mainAudio && !mainAudio.paused) mainAudio.pause();
      const audio = document.getElementById('tts-' + btn.dataset.id);
      if (!audio) return;
      audio.playbackRate = currentRate;
      btn.classList.add('playing');
      audio.play();
      audio.onended = () => btn.classList.remove('playing');
    }});
  }});
}})();
</script>
</body>
</html>
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input", help="Input JSON (see SKILL.md schema)")
    ap.add_argument("output_dir", help="Directory for index.html + *.mp3 (TTS)")
    ap.add_argument("--voice", default=DEFAULT_VOICE,
                    help=f"edge-tts voice (default {DEFAULT_VOICE})")
    ap.add_argument("--no-audio", action="store_true",
                    help="Skip TTS generation")
    ap.add_argument("--overwrite-tts", action="store_true",
                    help="Regenerate TTS even if target mp3 exists")
    args = ap.parse_args()

    data = json.loads(Path(args.input).read_text(encoding="utf-8"))
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    sentences = data.get("sentences", [])
    if not sentences:
        print("ERROR: no sentences in input", file=sys.stderr)
        sys.exit(1)

    if not args.no_audio:
        fillable = [s for s in sentences if s.get("words")]
        if fillable:
            print(f"Synthesizing TTS for {len(fillable)} filled sentence(s)...",
                  file=sys.stderr)
            asyncio.run(synth_all(fillable, out_dir, args.voice, args.overwrite_tts))
        else:
            print("No sentence has `words` filled yet; skipping TTS.", file=sys.stderr)

    (out_dir / "index.html").write_text(build_html(data), encoding="utf-8")
    print(f"Written: {out_dir / 'index.html'}")


if __name__ == "__main__":
    main()
