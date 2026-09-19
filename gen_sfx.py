#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成占位音效（纯 stdlib，无外部依赖）。木鱼敲击用衰减正弦合成，胡牌用上扬三音。
输出到 static/sfx/*.wav —— 今晚只求能响，明天可换更好的素材。"""
import math
import os
import struct
import wave

SR = 16000
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "sfx")
os.makedirs(OUT, exist_ok=True)


def write_wav(name, samples):
    path = os.path.join(OUT, name)
    with wave.open(path, "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        frames = b"".join(
            struct.pack("<h", max(-32767, min(32767, int(s * 32767)))) for s in samples
        )
        w.writeframes(frames)
    return path


def tone(freq, dur, vol=0.5, decay=6.0):
    n = max(1, int(SR * dur))
    return [vol * math.sin(2 * math.pi * freq * i / SR) * math.exp(-decay * i / n)
            for i in range(n)]


def gap(dur):
    return [0.0] * int(SR * dur)


def seq(*chunks):
    out = []
    for c in chunks:
        out += c
    return out


SOUNDS = {
    "draw.wav":    tone(760, 0.09, 0.40, 14),                       # 摸牌：轻脆一点
    "discard.wav": tone(300, 0.11, 0.55, 12),                       # 打牌：低一点的“嗒”
    "peng.wav":    seq(tone(520, 0.07, 0.60, 16), gap(0.03),        # 碰：木鱼双击
                       tone(520, 0.09, 0.55, 14)),
    "gang.wav":    seq(tone(440, 0.07, 0.60, 15), gap(0.02),        # 杠：三连击更重
                       tone(440, 0.07, 0.60, 15), gap(0.02),
                       tone(330, 0.13, 0.60, 10)),
    "hu.wav":      seq(tone(523, 0.12, 0.42, 4), tone(659, 0.12, 0.42, 4),
                       tone(784, 0.24, 0.46, 3)),                    # 胡：上扬三音
    "turn.wav":    seq(tone(880, 0.10, 0.35, 6), gap(0.02),          # 到你了：两声轻提示
                       tone(1175, 0.14, 0.30, 5)),
}

if __name__ == "__main__":
    for name, samples in SOUNDS.items():
        p = write_wav(name, samples)
        print(f"  {name}  {os.path.getsize(p)} bytes")
    print("done ->", OUT)
