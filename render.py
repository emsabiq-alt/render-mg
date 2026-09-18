"""
render.py — Deterministic Headless Chromium Frame Renderer & Audio Stitcher.
Support parallel rendering by scene range or frame range for GitHub Actions matrix.
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
        # Linux
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
    parser.add_argument("--html", default="kapal-virgo.html", help="Path HTML")
    parser.add_argument("--audio-dir", default="audio", help="Folder file scXX.mp3")
    parser.add_argument("--out", default="output/kapal_virgo_1080p.mp4", help="Output MP4")
    parser.add_argument("--fps", type=int, default=30, help="Frame rate")
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument("--start-scene", type=int, default=1, help="Scene awal (1-indexed)")
    parser.add_argument("--end-scene", type=int, default=50, help="Scene akhir (1-indexed)")
    parser.add_argument("--chunk-name", default="chunk", help="Nama chunk file")
    args = parser.parse_args()

    html_file = Path(args.html).resolve()
    audio_dir = Path(args.audio_dir).resolve()
    out_file = Path(args.out).resolve()
    out_file.parent.mkdir(parents=True, exist_ok=True)

    html_content = html_file.read_text(encoding="utf-8")
    m = re.search(r"const AI_NARRATION = (\[.*?\]);\s*const SCENE_DURATIONS", html_content, re.DOTALL)
    if not m:
        raise RuntimeError("AI_NARRATION tidak ditemukan!")
    narr = json.loads(m.group(1))

    # Hitung waktu start & durasi per scene
    scene_durations = [float(s.get("durationSec", 17.0)) for s in narr]
    scene_starts = []
    tot = 0.0
    for d in scene_durations:
        scene_starts.append(tot)
        tot += d

    total_film_dur = sum(scene_durations)

    s_idx = max(0, args.start_scene - 1)
    e_idx = min(len(narr), args.end_scene)

    start_time = scene_starts[s_idx]
    end_time = scene_starts[e_idx - 1] + scene_durations[e_idx - 1]
    chunk_dur = end_time - start_time
    total_frames = int(chunk_dur * args.fps)

    print(f"==================================================")
    print(f"RENDER SEGMENT: Scene {args.start_scene} s/d {args.end_scene}")
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
    audio_concat_file = tmp_dir / "audio_concat.txt"
    with open(audio_concat_file, "w", encoding="utf-8") as f:
        for sc in range(args.start_scene, args.end_scene + 1):
            sc_file = audio_dir / f"sc{sc:02d}.mp3"
            if sc_file.exists():
                f.write(f"file '{sc_file.resolve().as_posix()}'\n")

    chunk_audio = tmp_dir / "chunk_audio.m4a"
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
        "-i", str(chunk_audio),
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "18",
        "-pix_fmt", "yuv420p",
        "-c:a", "copy",
        "-movflags", "+faststart",
        "-shortest",
        str(out_file)
    ]
    subprocess.run(ffmpeg_cmd, check=True)
    print(f"Berhasil membuat: {out_file} ({out_file.stat().st_size / (1024*1024):.1f} MB)\n")

    shutil.rmtree(tmp_dir, ignore_errors=True)

if __name__ == "__main__":
    main()
