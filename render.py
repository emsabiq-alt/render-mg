"""
render.py — Deterministic Headless Chromium Frame Renderer & Audio Stitcher.
Support parallel rendering by scene range or frame range for GitHub Actions matrix.
Supports both a041 project.json/render.html and legacy standalone explainer HTML.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
import urllib.request
import websocket


def find_chrome():
    candidates = [
        # Linux (GitHub Actions Runner)
        "google-chrome",
        "google-chrome-stable",
        "chromium",
        "chromium-browser",
        "/usr/bin/google-chrome",
        "/usr/bin/chromium-browser",
        # Windows
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    ]
    for c in candidates:
        if shutil.which(c) or Path(c).exists():
            return c
    raise RuntimeError("Chrome / Chromium tidak ditemukan di sistem!")


class CDP:
    def __init__(self, port: int):
        ws_url = None
        for _ in range(40):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=2) as r:
                    for t in json.loads(r.read()):
                        if t.get("type") == "page":
                            ws_url = t.get("webSocketDebuggerUrl")
                            break
                if ws_url:
                    break
            except Exception:
                pass
            time.sleep(0.5)
        if not ws_url:
            raise RuntimeError("Gagal menghubungkan WebSocket CDP Chrome!")

        self.ws = websocket.create_connection(ws_url, timeout=120)
        self.n = 0

    def call(self, method: str, **params):
        self.n += 1
        self.ws.send(json.dumps({"id": self.n, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == self.n:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})

    def eval(self, expr: str):
        res = self.call("Runtime.evaluate", expression=expr, returnByValue=True, awaitPromise=True)
        return res.get("result", {}).get("value")

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--html", default="", help="Path HTML")
    parser.add_argument("--audio-dir", default="audio", help="Folder file scXX.mp3")
    parser.add_argument("--project-json", default="project.json", help="Path project.json")
    parser.add_argument("--out", default="output/rendered_video.mp4", help="Output MP4")
    parser.add_argument("--fps", type=int, default=30, help="Frame rate")
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument("--start-scene", type=int, default=1, help="Scene awal (1-indexed)")
    parser.add_argument("--end-scene", type=int, default=50, help="Scene akhir (1-indexed)")
    parser.add_argument("--chunk-name", default="chunk", help="Nama chunk file")
    args = parser.parse_args()

    # 1. Tentukan berkas HTML
    html_file = None
    if args.html and Path(args.html).exists():
        html_file = Path(args.html).resolve()
    elif Path("render.html").exists():
        html_file = Path("render.html").resolve()
    elif Path("kapal-virgo.html").exists():
        html_file = Path("kapal-virgo.html").resolve()
    else:
        candidates = list(Path(".").glob("*.html"))
        if candidates:
            html_file = candidates[0].resolve()
        else:
            raise RuntimeError("Berkas HTML render tidak ditemukan!")

    audio_dir = Path(args.audio_dir).resolve()
    out_file = Path(args.out).resolve()
    out_file.parent.mkdir(parents=True, exist_ok=True)

    # 2. Tentukan durasi scene dan pemetaan audio
    scene_durations: list[float] = []
    audio_map: dict[int, str] = {}

    proj_json = Path(args.project_json) if args.project_json else Path("project.json")
    if proj_json.exists():
        try:
            p_data = json.loads(proj_json.read_text(encoding="utf-8"))
            scenes = p_data.get("scenes", [])
            for i, s in enumerate(scenes):
                dur = float(s.get("durasi") or 8.0)
                scene_durations.append(dur)
                audio_map[i + 1] = s.get("audio") or f"sc{i+1:02d}.mp3"
        except Exception as e:
            print(f"Peringatan membaca {proj_json}: {e}")
            scene_durations = []

    if not scene_durations:
        html_content = html_file.read_text(encoding="utf-8")
        # Format META (a041 player.py)
        m_meta = re.search(r"const META\s*=\s*(\[.*?\]);\s*const TOTAL", html_content, re.DOTALL)
        if m_meta:
            meta_items = json.loads(m_meta.group(1))
            for i, m_item in enumerate(meta_items):
                scene_durations.append(float(m_item.get("dur") or 8.0))
                audio_map[i + 1] = m_item.get("audio") or f"sc{i+1:02d}.mp3"
        else:
            # Format AI_NARRATION (legacy kapal-virgo.html)
            m_narr = re.search(r"const AI_NARRATION = (\[.*?\]);\s*const SCENE_DURATIONS", html_content, re.DOTALL)
            if not m_narr:
                raise RuntimeError("Durasi scene tidak ditemukan di HTML maupun project.json!")
            narr = json.loads(m_narr.group(1))
            for i, s in enumerate(narr):
                scene_durations.append(float(s.get("durationSec", 17.0)))
                audio_map[i + 1] = f"sc{i+1:02d}.mp3"

    total_scenes = len(scene_durations)
    scene_starts = []
    tot = 0.0
    for d in scene_durations:
        scene_starts.append(tot)
        tot += d

    total_film_dur = sum(scene_durations)

    s_idx = max(0, args.start_scene - 1)
    e_idx = min(total_scenes, args.end_scene)

    if s_idx >= total_scenes:
        print(f"Scene awal ({args.start_scene}) melebihi total scene ({total_scenes}). Skip.")
        cmd_blank = [
            "ffmpeg", "-y", "-f", "lavfi", "-i", f"color=c=black:s={args.width}x{args.height}:d=0.1",
            "-f", "lavfi", "-i", "anullsrc=cl=stereo:r=44100", "-shortest",
            "-c:v", "libx264", "-c:a", "aac", str(out_file)
        ]
        subprocess.run(cmd_blank, check=True)
        return

    start_time = scene_starts[s_idx]
    end_time = scene_starts[e_idx - 1] + scene_durations[e_idx - 1]
    chunk_dur = end_time - start_time
    total_frames = int(chunk_dur * args.fps)

    print(f"==================================================")
    print(f"FILE HTML     : {html_file.name}")
    print(f"RENDER CHUNK  : Scene {s_idx + 1} s/d {e_idx} (dari total {total_scenes} scene)")
    print(f"Rentang Waktu : {start_time:.3f}s - {end_time:.3f}s ({chunk_dur:.2f}s)")
    print(f"Total Frame   : {total_frames} @ {args.fps} FPS ({args.width}x{args.height})")
    print(f"==================================================")

    tmp_dir = Path(tempfile.mkdtemp(prefix="mg_render_"))
    frames_dir = tmp_dir / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    profil_dir = tmp_dir / "chrome_profile"

    chrome = find_chrome()
    port = 9388 + (args.start_scene % 50)

    cmd_chrome = [
        chrome,
        "--headless=new",
        f"--remote-debugging-port={port}",
        "--remote-allow-origins=*",
        f"--user-data-dir={profil_dir}",
        f"--window-size={args.width},{args.height}",
        "--hide-scrollbars",
        "--force-device-scale-factor=1",
        "--disable-gpu-vsync",
        "--run-all-compositor-stages-before-draw",
        "--disable-background-timer-throttling",
        "--mute-audio",
        "--no-first-run",
        "--no-default-browser-check",
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-dev-shm-usage",
        html_file.as_uri(),
    ]

    proc = subprocess.Popen(cmd_chrome, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    t_start = time.time()

    try:
        cdp = CDP(port)
        cdp.call("Page.enable")
        cdp.call("Emulation.setDeviceMetricsOverride", width=args.width, height=args.height, deviceScaleFactor=1, mobile=False)

        # Tunggu browser siap
        print("Menunggu inisialisasi browser & font...")
        for _ in range(60):
            if cdp.eval("!!window.SIAP_RENDER && document.fonts.status==='loaded'"):
                break
            time.sleep(0.5)
        time.sleep(1.0)
        cdp.eval("if (window.stopTicker) window.stopTicker(); window.IS_RENDERING = true;")

        print("Mulai render frame...")
        for i in range(total_frames):
            cur_t = start_time + (i / args.fps)
            cdp.eval(f"window.renderAt({cur_t:.5f})")
            shot = cdp.call("Page.captureScreenshot", format="jpeg", quality=90, captureBeyondViewport=False)
            frame_path = frames_dir / f"{i:06d}.jpg"
            frame_path.write_bytes(base64.b64decode(shot["data"]))

            if (i + 1) % 150 == 0 or i == total_frames - 1:
                elapsed = time.time() - t_start
                fps_rate = (i + 1) / elapsed if elapsed > 0 else 0.1
                remaining_f = total_frames - (i + 1)
                eta_s = remaining_f / fps_rate if fps_rate > 0 else 0
                pct = ((i + 1) / total_frames) * 100
                print(f"[{pct:5.1f}%] Frame {i+1}/{total_frames} | Speed: {fps_rate:.2f} fps | ETA: {int(eta_s//60)}m {int(eta_s%60)}s", flush=True)

        cdp.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()

    render_elapsed = time.time() - t_start
    avg_fps = total_frames / render_elapsed if render_elapsed > 0 else 0
    print(f"\nRender selesai dalam {render_elapsed:.1f} detik (Rata-rata: {avg_fps:.2f} fps)!")

    # Gabung audio untuk scene dalam rentang ini
    chunk_audio = tmp_dir / "chunk_audio.m4a"
    ada_audio = False

    # 1. Cek apakah ada master_audio.m4a (audio penuh: VO + BGM + SFX foley)
    master_audio_file = audio_dir / "master_audio.m4a"
    if master_audio_file.exists():
        chunk_dur = end_time - start_time
        res_slice = subprocess.run([
            "ffmpeg", "-y",
            "-ss", f"{start_time:.3f}",
            "-t", f"{chunk_dur:.3f}",
            "-i", str(master_audio_file),
            "-c:a", "copy",
            str(chunk_audio)
        ], capture_output=True)
        if chunk_audio.exists() and chunk_audio.stat().st_size > 2000:
            ada_audio = True
            print(f"Menggunakan irisan master audio (VO + BGM + SFX) dari {start_time:.2f}s s/d {end_time:.2f}s")

    # 2. Fallback: Concat narasi individual jika master_audio belum ada
    if not ada_audio:
        audio_concat_file = tmp_dir / "audio_concat.txt"
        with open(audio_concat_file, "w", encoding="utf-8") as f:
            for sc in range(s_idx + 1, e_idx + 1):
                sc_audio_name = audio_map.get(sc, f"sc{sc:02d}.mp3")
                sc_file = audio_dir / sc_audio_name
                if sc_file.exists():
                    f.write(f"file '{sc_file.resolve().as_posix()}'\n")
                    ada_audio = True

        if ada_audio:
            subprocess.run([
                "ffmpeg", "-y", "-f", "concat", "-safe", "0",
                "-i", str(audio_concat_file),
                "-c:a", "aac", "-b:a", "192k",
                str(chunk_audio)
            ], check=True, capture_output=True)

    print("Menggabungkan frame + audio dengan FFmpeg...")
    ffmpeg_cmd = [
        "ffmpeg", "-y",
        "-framerate", str(args.fps),
        "-i", str(frames_dir / "%06d.jpg"),
    ]
    if ada_audio and chunk_audio.exists():
        ffmpeg_cmd += ["-i", str(chunk_audio), "-c:a", "copy"]
    else:
        ffmpeg_cmd += ["-f", "lavfi", "-i", "anullsrc=cl=stereo:r=44100", "-c:a", "aac", "-shortest"]

    ffmpeg_cmd += [
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "18",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        "-shortest",
        str(out_file)
    ]
    subprocess.run(ffmpeg_cmd, check=True)
    print(f"Berhasil membuat: {out_file} ({out_file.stat().st_size / (1024*1024):.1f} MB)\n")

    shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
