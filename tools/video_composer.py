#!/usr/bin/env python3
"""
视频合成器 v2 — 高效版
"""

import os, sys, subprocess, shutil

FRAMES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "reports", "ETF", "video_frames")
OUTPUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "reports", "ETF", "每日复盘-20260727.mp4")

W, H = 1080, 1920
FPS = 30

FRAMES = [
    ("frame_01_index_cards.png",     5.0),
    ("frame_02_changxin_intro.png",  6.0),
    ("frame_03_pie.png",             5.0),
    ("frame_04_market_cap.png",      5.5),
    ("frame_05_advance_decline.png", 5.0),
    ("frame_06_sz50_divergence.png", 6.0),
    ("frame_07_dragon_tiger.png",    5.0),
    ("frame_08_pe_gauge.png",        6.0),
    ("frame_09_conclusion.png",      4.5),
    ("frame_10_signals.png",         6.0),
]
TOTAL_DUR = sum(d for _, d in FRAMES)

def build():
    print(f"🎬 合成视频 — 总计 {TOTAL_DUR:.1f}s, {len(FRAMES)} 帧")
    temp_dir = os.path.join(FRAMES_DIR, "_temp")
    os.makedirs(temp_dir, exist_ok=True)
    clip_files = []

    for i, (fname, dur) in enumerate(FRAMES):
        path = os.path.join(FRAMES_DIR, fname)
        out_clip = os.path.join(temp_dir, f"c{i:02d}.mp4")
        clip_files.append(out_clip)

        if os.path.exists(out_clip) and os.path.getsize(out_clip) > 10000:
            print(f"  ⏩ c{i:02d} 已存在 ({dur}s)")
            continue

        total_frames = int(dur * FPS)
        # Ken Burns: 从 scale=1.0 慢慢放大到 1.05
        vf = (f"scale=iw*1.2:ih*1.2,"
              f"zoompan=z='min(zoom+0.001,1.08)':d={total_frames}:s={W}x{H}:fps={FPS},"
              f"format=yuv420p")
        cmd = [
            "ffmpeg", "-y", "-loop", "1", "-t", str(dur),
            "-i", path,
            "-vf", vf,
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-pix_fmt", "yuv420p", "-an",
            out_clip
        ]
        print(f"  🎞️  c{i:02d} ({dur}s)...")
        subprocess.run(cmd, check=True, capture_output=True)
        sz = os.path.getsize(out_clip) / 1024 / 1024
        print(f"       {sz:.1f} MB")

    # concat
    concat_txt = os.path.join(temp_dir, "list.txt")
    with open(concat_txt, "w") as f:
        for cf in clip_files:
            f.write(f"file '{cf}'\n")

    print(f"\n🔗 合并片段 → {OUTPUT}")
    subprocess.run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_txt,
        "-c", "copy", "-movflags", "+faststart", OUTPUT
    ], check=True, capture_output=True)

    shutil.rmtree(temp_dir)
    sz = os.path.getsize(OUTPUT) / 1024 / 1024
    print(f"\n✅ 视频完成: {OUTPUT}")
    print(f"   {TOTAL_DUR:.0f}s | {W}x{H} | 9:16 | {sz:.1f} MB")

if __name__ == "__main__":
    build()
