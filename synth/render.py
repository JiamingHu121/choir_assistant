"""合成音频：MusicXML → MIDI → WAV。

- MusicXML → MIDI：music21（成熟开源符号乐谱库），按声部把合唱声部设为
  GM 91(Choir Aahs)、钢琴设为 GM 0(Acoustic Grand)。
- MIDI → WAV：FluidSynth + GM 音色库（sf2）。
- 可选 WAV → MP3：ffmpeg。
"""
from __future__ import annotations

import os
import subprocess
from typing import Optional

from runtime import find_binary, find_soundfont

try:
    from music21 import converter, instrument, midi
    MUSIC21_OK = True
except Exception as e:  # pragma: no cover
    MUSIC21_OK = False
    _music21_err = e


def _gm_instrument(program: int):
    """按 GM program 号构造乐器对象（不依赖具体类名，保证跨版本可用）。"""
    inst = instrument.Instrument()
    inst.midiProgram = program
    return inst

VOICE_NAMES = ("soprano", "s", "alto", "a", "tenor", "t", "bass", "b", "choir",
               "女高", "女低", "男高", "男低", "声乐", "人声")
PIANO_NAMES = ("piano", "pno", "伴奏", "钢琴", "键盘")


def musicxml_to_midi(mxl_path: str, out_mid: str,
                     voice_program: int = 91,
                     piano_program: int = 0) -> Optional[str]:
    """读取 MusicXML(.mxl/.xml)，按声部分配独立 MIDI 轨道/通道/音色，写出 MIDI。

    不用 music21 的 MIDI 翻译（其通道分配不可控），改为：music21 负责解析音符，
    mido 负责写出，保证每个声部一条轨、一个独立通道、一个 GM program。
    连音（tie）自动合并为长音。
    """
    if not MUSIC21_OK:
        return None
    import mido  # 轻量 MIDI 读写库

    score = converter.parse(mxl_path, format="musicxml")

    # Expand written repeats and merge ties within their original voices.
    try:
        score = score.expandRepeats()
    except Exception:
        pass
    score = score.stripTies(inPlace=False)
    tpb = 960
    midi_file = mido.MidiFile(ticks_per_beat=tpb)
    meta_track = mido.MidiTrack()
    midi_file.tracks.append(meta_track)
    tempo_events = {0: mido.bpm2tempo(120)}
    for offset, _end, mark in score.metronomeMarkBoundaries():
        bpm = mark.getQuarterBPM()
        if bpm:
            tempo_events[round(float(offset) * tpb)] = mido.bpm2tempo(bpm)
    last = 0
    for tick, tempo in sorted(tempo_events.items()):
        meta_track.append(mido.MetaMessage("set_tempo", tempo=tempo, time=tick-last))
        last = tick
    free_chans = [c for c in range(16) if c != 9]
    if len(score.parts) > len(free_chans):
        raise ValueError("超过 15 个声部，无法分配独立 MIDI 通道")
    for part_idx, part in enumerate(score.parts):
        name = f"{part.partName or ''} {part.id or ''}"
        prog = piano_program if any(k in name.lower() for k in PIANO_NAMES) else voice_program
        ch = free_chans[part_idx]
        events = []
        for n in part.recurse().notes:
            onset = round(float(n.getOffsetInHierarchy(part)) * tpb)
            end = onset + round(float(n.duration.quarterLength) * tpb)
            if end <= onset:
                continue
            for pitch in n.pitches:
                events.append((onset, 1, int(pitch.midi)))
                events.append((end, 0, int(pitch.midi)))
        events.sort()  # Release preceding notes before retriggering the same pitch.
        track = mido.MidiTrack()
        track.append(mido.MetaMessage("track_name", name=name.strip(), time=0))
        track.append(mido.Message("program_change", program=prog, channel=ch, time=0))
        last = 0
        for tick, kind, pitch in events:
            track.append(mido.Message("note_on" if kind else "note_off", note=pitch,
                                      velocity=80 if kind else 0, channel=ch, time=tick-last))
            last = tick
        midi_file.tracks.append(track)

    midi_file.save(out_mid)
    return out_mid


def _same_pitch(a, b) -> bool:
    try:
        return a.pitch.midi == b.pitch.midi
    except Exception:
        return False


def _tie_continues(n) -> bool:
    """n 是 tie 的中间/结尾音符时返回 True（应并入前一个音）。"""
    try:
        t = n.tie
    except Exception:
        return False
    if t is None:
        return False
    return getattr(t, "type", None) in ("continue", "stop")


def _fluidsynth_path() -> Optional[str]:
    """Discover a separately installed executable or an explicit override."""
    return find_binary("fluidsynth", required=False)


def midi_to_wav(mid_path: str, soundfont: str, out_wav: str,
                gain: float = 0.5, fluidsynth: Optional[str] = None) -> str:
    """用 FluidSynth 离线渲染 MIDI → WAV（-F 必须置于位置参数之前）。"""
    fs = find_binary("fluidsynth", fluidsynth)
    soundfont = find_soundfont(soundfont)
    env = dict(os.environ)
    cmd = [fs, "-F", out_wav, "-ni", "-g", str(gain), soundfont, mid_path]
    r = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if r.returncode != 0 or not os.path.exists(out_wav):
        raise RuntimeError(f"FluidSynth 渲染失败:\n{(r.stderr or r.stdout)[-1200:]}")
    return out_wav


def _trim_trailing_silence(wav_path: str, ffmpeg: Optional[str] = None) -> str:
    """裁剪尾部静音（FluidSynth 渲染会在曲末附加约 5s 静音），结尾保留 0.35s。"""
    ff = find_binary("ffmpeg", ffmpeg, required=False)
    if not ff:
        return wav_path
    tmp = wav_path + ".trim.wav"
    filt = "areverse,silenceremove=start_periods=1:start_threshold=-50dB:start_duration=0.1,areverse,apad=pad_dur=0.35"
    try:
        r = subprocess.run(
            [ff, "-y", "-i", wav_path, "-af", filt, "-c:a", "pcm_s16le", tmp],
            capture_output=True, text=True,
        )
        if r.returncode == 0 and os.path.exists(tmp) and os.path.getsize(tmp) > 1024:
            os.replace(tmp, wav_path)
    except Exception:
        pass
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
    return wav_path


def wav_to_mp3(wav_path: str, mp3_path: str, ffmpeg: Optional[str] = None) -> str:
    """ffmpeg 转 MP3（可选）。"""
    ff = find_binary("ffmpeg", ffmpeg, required=False)
    if not ff:
        raise RuntimeError("未找到 ffmpeg")
    subprocess.run(
        [ff, "-y", "-i", wav_path, "-b:a", "192k", mp3_path],
        check=True, capture_output=True,
    )
    return mp3_path


def render(mxl_path: str, out_mid: str, out_wav: str,
           soundfont: str, make_mp3: bool = True,
           voice_program: int = 91, piano_program: int = 0) -> dict:
    """一站式：MusicXML → MIDI → WAV(→MP3)。返回产物路径字典。"""
    results: dict = {}
    mid = musicxml_to_midi(mxl_path, out_mid, voice_program, piano_program)
    if mid:
        results["midi"] = mid
    else:
        raise RuntimeError(
            "music21 不可用，无法生成 MIDI。请安装依赖（见 README）"
            "或先由 Audiveris 导出 MIDI。"
        )
    results["wav"] = midi_to_wav(results["midi"], soundfont, out_wav)
    _trim_trailing_silence(results["wav"])
    if make_mp3:
        mp3 = wav_to_mp3(results["wav"], out_wav.rsplit(".", 1)[0] + ".mp3")
        results["mp3"] = mp3
    return results
