"""Test suite: physics, models, segmentation, alignment post-processing, scene rendering."""
from __future__ import annotations

import math
import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.audio.asr_align import (attach_kicks, enforce_monotonic, mark_keywords,  # noqa: E402
                                 snap_to_onsets)
from app.audio.classify import UniversalAudioClassifier  # noqa: E402
from app.audio.rhythm import analyze_rhythm  # noqa: E402
from app.audio.segment import PhraseSegmenter  # noqa: E402
from app.core.models import AudioProfile, Phrase, Project, SpringConfig, Style, Word  # noqa: E402
from app.render.physics import cubic_bezier_ease, get_word_transform, kick_pulse, spring  # noqa: E402
from app.render.presets import AutoStyleEngine  # noqa: E402


# ------------------------------------------------------------------- physics
def test_spring_boundaries():
    assert spring(0.0) == 0.0
    assert spring(-2.0) == 0.0
    assert abs(spring(20.0) - 1.0) < 1e-6


def test_underdamped_overshoots():
    vals = [spring(t / 2000, 320, 1.0, 16) for t in range(4000)]
    assert max(vals) > 1.02
    assert SpringConfig(k=320, c=16).zeta < 1.0


def test_critical_no_overshoot():
    c = 2 * math.sqrt(180.0)
    vals = [spring(t / 2000, 180, 1.0, c) for t in range(6000)]
    assert max(vals) <= 1.0 + 1e-9
    assert abs(SpringConfig(k=180, c=c).zeta - 1.0) < 1e-9


def test_overdamped_monotone():
    vals = [spring(t / 1000, 180, 1.0, 120) for t in range(3000)]
    assert all(b >= a - 1e-12 for a, b in zip(vals, vals[1:]))
    assert max(vals) <= 1.0 + 1e-9


def test_bezier_and_pulse():
    assert cubic_bezier_ease(0) == 0 and cubic_bezier_ease(1) == 1
    xs = [cubic_bezier_ease(i / 50) for i in range(51)]
    assert all(b >= a - 1e-9 for a, b in zip(xs, xs[1:]))
    assert kick_pulse(1.12, 1.0, 0.1, 0.12) == pytest.approx(0.1 * math.exp(-1), rel=1e-6)


def test_transform_is_stateless():
    w = Word(text="hi", start=1.0, end=1.5)
    s = Style()
    a = get_word_transform(1.2, w, s, 1.05)
    b = get_word_transform(1.2, w, s, 1.05)
    assert a == b
    assert 0.0 <= a["opacity"] <= 1.0


# -------------------------------------------------------------------- models
def test_word_repairs_inverted_times():
    w = Word(text="x", start=2.0, end=1.0)
    assert w.end > w.start


def test_project_roundtrip(tmp_path):
    proj = Project(
        video_path="a.mp4", duration=12.0,
        audio_profile=AudioProfile(genre="rap", bpm=88.0, onsets=[0.1, 0.5]),
        phrases=[Phrase(words=[Word(text="эй", start=0.0, end=0.3)])],
        global_style=AutoStyleEngine.get_style_for_genre("rap"),
    )
    p = tmp_path / "p.ctp"
    proj.save(p)
    back = Project.load(p)
    assert back.model_dump() == proj.model_dump()


def test_style_rejects_bad_color():
    with pytest.raises(Exception):
        Style(fill_color="red")


# -------------------------------------------------------------- postprocessing
def _words(spec):
    return [Word(text=t, start=s, end=e) for t, s, e in spec]


def test_monotonic_and_min_duration():
    ws = enforce_monotonic(_words([("a", 0.0, 0.5), ("b", 0.3, 0.31), ("c", 0.2, 0.9)]))
    assert all(w.end > w.start for w in ws)
    assert all(b.start >= a.end - 1e-9 for a, b in zip(ws, ws[1:]))


def test_snap_to_onsets():
    ws = _words([("a", 0.52, 0.8)])
    snap_to_onsets(ws, [0.50, 1.0], tol=0.04)
    assert ws[0].start == pytest.approx(0.50)
    ws2 = _words([("a", 0.62, 0.9)])
    snap_to_onsets(ws2, [0.50], tol=0.04)
    assert ws2[0].start == pytest.approx(0.62)     # too far -> untouched


def test_attach_kicks_and_keywords():
    ws = _words([("революция", 0.0, 0.6), ("и", 0.6, 0.7)])
    attach_kicks(ws, [0.2, 3.0])
    assert ws[0].kick == pytest.approx(0.2)
    mark_keywords(ws)
    assert ws[0].is_keyword and not ws[1].is_keyword


# ------------------------------------------------------------- segmentation
def _stream(n, step=0.3):
    return [Word(text=f"w{i}", start=i * step, end=i * step + step * 0.8) for i in range(n)]


def test_rap_segments_are_short():
    prof = AudioProfile(genre="rap")
    ph = PhraseSegmenter().segment_words(_stream(20), prof)
    assert ph and all(len(p.words) <= 2 for p in ph)
    assert sum(len(p.words) for p in ph) == 20


def test_speech_segments_are_longer():
    prof = AudioProfile(genre="speech_podcast")
    ph = PhraseSegmenter().segment_words(_stream(20, 0.35), prof)
    assert max(len(p.words) for p in ph) >= 3
    assert sum(len(p.words) for p in ph) == 20


def test_segmentation_respects_pauses():
    ws = _stream(6, 0.3)
    ws[3].start += 1.2          # big gap before w3
    ws[3].end += 1.2
    for w in ws[4:]:
        w.start += 1.2
        w.end += 1.2
    ph = PhraseSegmenter().segment_words(ws, AudioProfile(genre="speech_podcast"))
    boundaries = {p.words[0].text for p in ph}
    assert "w3" in boundaries


def test_empty_input():
    assert PhraseSegmenter().segment_words([], None) == []


# ---------------------------------------------------------------- audio dsp
def _click_track(bpm=120.0, sr=22050, dur=6.0):
    t = np.arange(int(sr * dur)) / sr
    y = 0.02 * np.random.RandomState(0).randn(t.size).astype(np.float32)
    period = 60.0 / bpm
    for k in range(int(dur / period)):
        i = int(k * period * sr)
        env = np.exp(-np.arange(2000) / 300.0)
        y[i:i + 2000] += (env * np.sin(2 * np.pi * 60 * np.arange(2000) / sr)).astype(np.float32)
    return y


def test_rhythm_finds_tempo():
    r = analyze_rhythm(_click_track(120.0))
    assert 55 <= r.bpm <= 250
    assert len(r.onsets) > 5
    assert r.low_energy > 0.0
    assert 0.0 <= r.beat_regularity <= 1.0


def test_classifier_rules_give_distribution():
    f = UniversalAudioClassifier.features(analyze_rhythm(_click_track()), words_per_sec=4.5)
    probs = UniversalAudioClassifier.rule_scores(f)
    assert set(probs) == {"rap", "deep_house_phonk", "pop_dance", "chanson_acoustic",
                          "speech_podcast"}
    assert abs(sum(probs.values()) - 1.0) < 1e-3


def test_speechlike_features_map_to_speech():
    f = dict(bpm=0.0, onset_density=0.4, kick_rate=0.02, snare_rate=0.02, beat_regularity=0.0,
             low_energy=0.05, mid_energy=0.6, high_energy=0.35, spectral_centroid=1800,
             zcr=0.08, flatness=0.3, rms_std=0.02, rms_mean=0.04, silence_ratio=0.45,
             words_per_sec=2.3)
    assert max(UniversalAudioClassifier.rule_scores(f), key=lambda k: _p(f)[k]) == "speech_podcast"


def _p(f):
    return UniversalAudioClassifier.rule_scores(f)


# --------------------------------------------------------------- rendering
def _demo_project():
    words = [Word(text=w, start=i * 0.4, end=i * 0.4 + 0.35)
             for i, w in enumerate(["Кинетика", "в", "кадре"])]
    return Project(video_path="", duration=3.0, width=540, height=960,
                   phrases=[Phrase(words=words)],
                   global_style=AutoStyleEngine.get_style_for_genre("rap"))


def test_scene_evaluate_and_determinism(qapp):
    from app.render.scene import SubtitleScene
    scene = SubtitleScene(_demo_project())
    a = scene.evaluate(0.5)
    b = scene.evaluate(0.5)
    assert len(a) == len(b) and a[0]["text"] == b[0]["text"]
    assert scene.evaluate(50.0) == []


def test_render_rgba_size_and_nonempty(qapp):
    from app.render.scene import SubtitleScene
    scene = SubtitleScene(_demo_project())
    raw = scene.render_rgba(0.6)
    assert len(raw) == scene.width * scene.height * 4
    arr = np.frombuffer(raw, np.uint8).reshape(scene.height, scene.width, 4)
    assert arr[:, :, 3].max() > 0        # something was actually drawn


def test_smart_contrast_switches_to_black_on_white_bg(qapp):
    from app.render.effects_contrast import SmartContrastAI
    ai = SmartContrastAI()
    ai.update_background(np.full((16, 16, 3), 255, np.uint8))
    for _ in range(12):
        ai.update_background(np.full((16, 16, 3), 255, np.uint8))
    d = ai.decide(Style(fill_color="#FFFFFF"))
    assert d.fill_color == "#000000"
    assert d.ratio >= 4.5


def test_auto_style_differs_per_genre():
    rap = AutoStyleEngine.get_style_for_genre("rap")
    acoustic = AutoStyleEngine.get_style_for_genre("chanson_acoustic")
    assert rap.spring.k > acoustic.spring.k
    assert rap.words_per_block < acoustic.words_per_block
    assert rap.uppercase and not acoustic.uppercase


# ------------------------------------------------------- decoding fallbacks
def test_decoder_fallback_without_ffmpeg(tmp_path, monkeypatch):
    """With no ffmpeg binary at all, decoding must still work through PyAV."""
    from app.audio import extract
    pytest.importorskip("av")
    sr = 22050
    t = np.arange(sr * 2) / sr
    y = (0.3 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
    wav = extract.write_wav(tmp_path / "a.wav", y, sr)

    monkeypatch.setattr(extract, "find_ffmpeg", lambda: None)
    monkeypatch.setattr(extract, "find_ffprobe", lambda: None)
    pcm = extract.decode_pcm(wav, sr=16000)
    assert pcm.dtype == np.float32 and abs(len(pcm) / 16000 - 2.0) < 0.1
    assert 0.1 < float(np.abs(pcm).max()) <= 1.0
    assert abs(extract.ffprobe_duration(wav) - 2.0) < 0.15
    assert extract.probe_video(wav) == (1080, 1920, 30.0)      # audio-only input
    assert "PyAV" in extract.backend_report()


def test_ffmpeg_binary_raises_actionable_message(monkeypatch):
    from app.audio import extract
    monkeypatch.setattr(extract, "find_ffmpeg", lambda: None)
    with pytest.raises(extract.AudioDecodeError) as e:
        extract.ffmpeg_binary()
    assert "static-ffmpeg" in str(e.value)


def test_missing_file_is_reported(tmp_path):
    from app.audio.extract import AudioDecodeError, decode_pcm
    with pytest.raises(AudioDecodeError):
        decode_pcm(str(tmp_path / "nope.wav"))


def test_ensure_ffmpeg_never_raises():
    from app.audio.extract import ensure_ffmpeg
    ensure_ffmpeg(verbose=False)      # must not throw regardless of environment


# --------------------------------------------------- worker / thread safety
def test_worker_converts_exception_to_signal(qapp):
    from PySide6.QtCore import QCoreApplication
    from app.ui.main_window import Worker

    def boom(progress=None):
        raise RuntimeError("ffmpeg not found in PATH")

    w = Worker(boom)
    got: list[str] = []
    done: list[int] = []
    w.failed.connect(got.append)
    w.done.connect(lambda: done.append(1))
    w.run()                                   # must NOT propagate
    QCoreApplication.processEvents()
    assert got and "RuntimeError" in got[0]
    assert "static-ffmpeg" in got[0]          # actionable hint attached
    assert len(done) == 1


def test_worker_success_path(qapp):
    from app.ui.main_window import Worker
    seen: list[object] = []
    prog: list[tuple] = []
    w = Worker(lambda x, progress=None: (progress(0.5, "half"), x * 2)[1], 21)
    w.succeeded.connect(seen.append)
    w.progress.connect(lambda p, m: prog.append((p, m)))
    w.run()
    assert seen == [42] and prog == [(0.5, "half")]


# =================================== v2: ASR-движок для песен и новый UI ===
def test_clean_lyrics_strips_markup():
    from app.audio.audio_processor import clean_lyrics
    raw = "[Куплет 1]\nЯ иду по городу\nПрипев:\nСвет горит (x2)\n\n  тихо  "
    out = clean_lyrics(raw)
    assert "[" not in out and "x2" not in out
    assert out.startswith("Я иду по городу") and "Свет горит" in out
    assert "  " not in out


def test_drop_hallucinations_removes_long_repeats():
    from app.audio.audio_processor import drop_hallucinations
    ws = [Word(text="да", start=i * 0.2, end=i * 0.2 + 0.15) for i in range(10)]
    ws.append(Word(text="дальше", start=2.5, end=2.9))
    out = drop_hallucinations(ws, max_repeat=4)
    assert len(out) == 5                      # 4 повтора + следующее слово
    assert out[-1].text == "дальше"


def test_song_prompt_is_configured():
    from app.audio.audio_processor import SONG_PROMPT, SPEECH_PROMPT
    assert "песн" in SONG_PROMPT.lower() and "куплет" in SONG_PROMPT.lower()
    assert SONG_PROMPT != SPEECH_PROMPT


def test_capabilities_reports_all_engines():
    from app.audio.audio_processor import AudioProcessor
    caps = AudioProcessor().capabilities()
    assert set(caps) == {"demucs", "stable_ts", "faster_whisper", "torchaudio", "cuda", "ffmpeg"}
    assert all(isinstance(v, bool) for v in caps.values())


def test_vocal_separator_falls_back_without_demucs(tmp_path, monkeypatch):
    """Без Demucs изоляция всё равно обязана вернуть рабочий vocals.wav."""
    from app.audio import extract
    from app.audio.audio_processor import VocalSeparator
    sr = 16000
    t = np.arange(sr * 2) / sr
    mix = (0.4 * np.sin(2 * np.pi * 60 * t) +      # «бас»
           0.3 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)   # «голос»
    src = extract.write_wav(tmp_path / "mix.wav", mix, sr)

    monkeypatch.setattr(VocalSeparator, "demucs_available", staticmethod(lambda: False))
    res = VocalSeparator().separate(src, str(tmp_path / "out"))
    assert res.method == "hpss" and os.path.exists(res.vocals_path)
    voc = extract.decode_pcm(res.vocals_path, sr=sr)
    assert voc.size > sr and float(np.abs(voc).max()) > 0.05
    # низкочастотная энергия должна упасть относительно исходника
    def low_ratio(x):
        S = np.abs(np.fft.rfft(x[:sr]))
        f = np.fft.rfftfreq(sr, 1 / sr)
        return float(S[f < 120].sum() / (S.sum() + 1e-9))
    assert low_ratio(voc) < low_ratio(mix)


def test_engine_reports_backend_and_device():
    from app.audio.audio_processor import TranscriptionEngine, pick_device
    dev, comp = pick_device()
    assert dev in ("cpu", "cuda") and comp in ("int8", "float16")
    name = TranscriptionEngine("medium").backend_name()
    assert "medium" in name


def test_align_text_requires_lyrics():
    from app.audio.audio_processor import TranscriptionEngine
    with pytest.raises(ValueError):
        TranscriptionEngine().align_text("x.wav", "   ")


# ------------------------------------------------------------- пресеты UI
def test_style_presets_apply_all_fields():
    from app.render.style_presets import STYLE_BY_KEY, STYLE_PRESETS
    base = Style()
    assert len(STYLE_PRESETS) == 15
    assert len({p.key for p in STYLE_PRESETS}) == 15
    for p in STYLE_PRESETS:
        s = p.apply(base)
        assert isinstance(s, Style)
    yellow = STYLE_BY_KEY["capcut_yellow"].apply(base)
    assert yellow.keyword_color == "#FFD600" and yellow.uppercase
    minimal = STYLE_BY_KEY["minimal_dark_slate"].apply(base)
    assert minimal.card_enabled and minimal.stroke_width == 0
    assert STYLE_BY_KEY["typewriter_mono"].apply(base).animation == "typewriter"


def test_animation_presets_change_motion():
    from app.render.style_presets import ANIMATION_BY_KEY
    base = Style()
    bounce = ANIMATION_BY_KEY["bounce"].apply(base)
    static = ANIMATION_BY_KEY["static"].apply(base)
    fade = ANIMATION_BY_KEY["fade"].apply(base)
    assert bounce.spring.zeta < 1.0                 # отскок
    assert static.pop_in_per_word is False and static.karaoke is False
    assert fade.karaoke and fade.kick_pulse_amp == 0.0
    assert ANIMATION_BY_KEY["karaoke"].apply(base).karaoke is True


def test_position_presets_and_offset():
    from app.render.style_presets import apply_position, position_key_for
    s = Style()
    assert apply_position(s, "top").y_offset_pct == 0.18
    assert apply_position(s, "center").y_offset_pct == 0.50
    assert abs(apply_position(s, "bottom", 0.05).y_offset_pct - 0.83) < 1e-9
    assert apply_position(s, "bottom", 0.9).y_offset_pct <= 0.97      # клампится
    assert position_key_for(0.2) == "top" and position_key_for(0.8) == "bottom"


# -------------------------------------------------------------- UI v2 smoke
def test_ui_v2_builds_and_presets_work(qapp):
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from app.ui.ui_main import MainWindow
    w = MainWindow()

    assert w.tabs.count() == 3                       # Текст / Стиль / Анимация
    assert not w.advanced.content.isVisible()        # «сырые» параметры скрыты

    w._apply_style_preset("cyberpunk_neon")
    assert w.project.global_style.glow_radius >= 20
    w._apply_animation_preset("static")
    assert w.project.global_style.pop_in_per_word is False
    w._on_position_preset("top")
    assert w.project.global_style.y_offset_pct == 0.18
    w._on_offset(10)
    assert abs(w.project.global_style.y_offset_pct - 0.28) < 1e-9
    w._on_stroke_preset("bold")
    assert w.project.global_style.stroke_width == 11
    w.seg_aspect.set_value("16:9")
    w._update_style(safe_zone="16:9")
    assert w.project.global_style.safe_zone == "16:9"
    w.close()


def test_ui_v2_alignment_needs_media(qapp, monkeypatch):
    from PySide6.QtWidgets import QApplication, QMessageBox
    app = QApplication.instance() or QApplication([])
    from app.ui.ui_main import MainWindow
    shown: list[str] = []
    monkeypatch.setattr(QMessageBox, "information",
                        staticmethod(lambda *a, **k: shown.append(a[2] if len(a) > 2 else "")))
    monkeypatch.setattr(QMessageBox, "warning",
                        staticmethod(lambda *a, **k: shown.append(a[2] if len(a) > 2 else "")))
    w = MainWindow()
    w.run_alignment()                       # нет текста → подсказка, без падения
    assert shown and "текст" in shown[0].lower()
    w.txt_lyrics.setPlainText("строка песни")
    w.run_alignment()                       # нет файла → подсказка, без падения
    assert len(shown) == 2
    assert not w.runner.busy
    w.close()


def test_job_runner_error_does_not_crash(qapp):
    from PySide6.QtCore import QCoreApplication
    from app.ui.workers import JobRunner, friendly_error
    runner = JobRunner()
    errs: list[str] = []
    runner.failed.connect(errs.append)

    def boom(progress=None):
        raise RuntimeError("demucs is not installed")

    assert runner.start(boom)
    for _ in range(200):
        QCoreApplication.processEvents()
        if errs and not runner.busy:
            break
    assert errs and "pip install demucs" in errs[0]
    assert not runner.busy
    assert "stable-ts" in friendly_error(RuntimeError("stable_whisper missing"))


# ================ v3: фикс интро-галлюцинаций, плеер, синхронизация ========
def _w(text, a, b):
    return Word(text=text, start=a, end=b)


def test_detects_first_vocal_onset_after_intro():
    """30 с «музыкального интро» (тихо) → голос с 30-й секунды."""
    from app.core.asr_engine import detect_voice_activity
    sr = 16000
    intro = 0.002 * np.random.RandomState(1).randn(sr * 30).astype(np.float32)
    t = np.arange(sr * 5) / sr
    voice = (0.4 * np.sin(2 * np.pi * 220 * t) *
             (0.6 + 0.4 * np.sin(2 * np.pi * 4 * t))).astype(np.float32)
    pcm = np.concatenate([intro, voice])
    va = detect_voice_activity(pcm, use_silero=False)
    assert 29.0 < va.first_onset < 31.0
    assert va.segments and va.method == "energy"


def test_intro_hallucination_is_removed():
    """Кейс из баг-репорта: «Песня «Музыка»» растянута на 0–30 с."""
    from app.core.asr_engine import drop_intro_hallucinations
    words = [_w("Песня", 0.0, 15.0), _w("«Музыка»", 15.0, 30.0),
             _w("Первая", 30.4, 30.8), _w("строчка", 30.8, 31.3)]
    out = drop_intro_hallucinations(words, first_onset=30.2)
    assert [w.text for w in out] == ["Первая", "строчка"]
    assert abs(out[0].start - 30.2) < 0.06          # привязано к началу вокала


def test_real_words_in_intro_are_trimmed_not_deleted():
    from app.core.asr_engine import drop_intro_hallucinations
    words = [_w("куплет", 9.0, 10.4), _w("дальше", 10.5, 11.0)]
    out = drop_intro_hallucinations(words, first_onset=10.0)
    assert len(out) == 2 and out[0].start >= 9.6


def test_hallucination_patterns():
    from app.core.asr_engine import is_hallucination
    for bad in ["Песня «Музыка»", "Музыка", "[Music]", "Субтитры сделал DimaTorzok",
                "Спасибо за просмотр!", "...", "Продолжение следует..."]:
        assert is_hallucination(bad), bad
    for good in ["Привет", "город спит", "Я иду"]:
        assert not is_hallucination(good)


def test_max_segment_length_is_enforced():
    """Ни одна непрерывная фраза не может длиться дольше лимита."""
    from app.core.asr_engine import MAX_SEGMENT_SECONDS, enforce_max_segment
    words = [_w(f"w{i}", i * 0.8, i * 0.8 + 0.8) for i in range(20)]   # сплошные 16 с
    out = enforce_max_segment(words, MAX_SEGMENT_SECONDS)
    run_start = out[0].start
    longest = 0.0
    for a, b in zip(out, out[1:]):
        if b.start - a.end > 0.2:            # пауза = разрыв фразы
            longest = max(longest, a.end - run_start)
            run_start = b.start
    longest = max(longest, out[-1].end - run_start)
    assert longest <= MAX_SEGMENT_SECONDS + 0.5


def test_word_durations_are_clamped():
    from app.core.asr_engine import MAX_WORD_SECONDS, clamp_word_durations
    out = clamp_word_durations([_w("Музыка", 0.0, 30.0)])
    assert out[0].end - out[0].start == pytest.approx(MAX_WORD_SECONDS)


def test_vad_filter_drops_words_outside_speech():
    from app.core.asr_engine import filter_by_vad
    words = [_w("шум", 1.0, 2.0), _w("речь", 11.0, 11.5)]
    out = filter_by_vad(words, [(10.5, 14.0)])
    assert [w.text for w in out] == ["речь"]


def test_postprocess_full_chain_on_intro_case():
    from app.core.asr_engine import TranscriptionEngine, VoiceActivity
    eng = TranscriptionEngine("small")
    va = VoiceActivity(first_onset=30.2, segments=[(30.2, 60.0)], method="energy")
    words = [_w("Песня", 0.0, 12.0), _w("«Музыка»", 12.0, 30.0),
             _w("Первая", 30.3, 30.9), _w("строчка", 30.9, 31.4),
             _w("куплета", 31.4, 31.9)]
    out = eng.postprocess(words, va)
    assert [w.text for w in out] == ["Первая", "строчка", "куплета"]
    assert all(w.end > w.start for w in out)
    assert abs(out[0].start - 30.2) < 0.1


def test_align_anchors_first_word_to_first_vocal(tmp_path, monkeypatch):
    """Forced alignment: первое слово встаёт на первую секунду голоса, интро игнорируется."""
    from app.audio import extract
    from app.core import asr_engine
    from app.core.asr_engine import TranscriptionEngine

    sr = 16000
    intro = np.zeros(sr * 8, dtype=np.float32)
    t = np.arange(sr * 4) / sr
    voice = (0.4 * np.sin(2 * np.pi * 200 * t)).astype(np.float32)
    path = extract.write_wav(tmp_path / "vocals.wav", np.concatenate([intro, voice]), sr)

    class FakeAligner:
        def available(self):
            return True

        def align(self, pcm, seed):               # ровная раскладка по обрезку
            return [w.model_copy() for w in seed]

    monkeypatch.setattr(TranscriptionEngine, "stable_ts_available", staticmethod(lambda: False))
    monkeypatch.setattr(asr_engine, "MMSForcedAligner", FakeAligner)

    res = TranscriptionEngine("small").align_text(path, "[Куплет 1]\nпервая строка песни")
    assert res.words
    assert res.first_onset > 7.0                  # интро распознано как тишина
    assert abs(res.words[0].start - res.first_onset) < 0.1   # жёсткая привязка
    assert [w.text for w in res.words] == ["первая", "строка", "песни"]
    assert all(w.end - w.start <= 1.61 for w in res.words)


# ------------------------------------------------------------------- плеер
def test_player_builds_with_working_backend(qapp):
    from app.ui.player import VideoPlayer, multimedia_status
    p = VideoPlayer()
    assert p.backend_name() in {"sink", "widget", "graphics", "frames"}
    assert p.overlay is not None
    assert isinstance(multimedia_status(), str)
    p.seek(2.5)
    assert p.position() == pytest.approx(2.5)
    p.shutdown()


def test_player_overlay_draws_subtitles(qapp):
    from app.render.scene import SubtitleScene
    from app.ui.player import VideoPlayer
    words = [Word(text="Привет", start=0.0, end=1.0), Word(text="мир", start=1.0, end=2.0)]
    proj = Project(duration=3.0, width=540, height=960, phrases=[Phrase(words=words)],
                   global_style=AutoStyleEngine.get_style_for_genre("pop_dance"))
    p = VideoPlayer()
    p.resize(540, 960)
    p.set_scene(SubtitleScene(proj, 540, 960))
    p.overlay.resize(540, 960)
    p.overlay.set_time(0.6)
    pix = p.overlay.grab()
    assert not pix.isNull()
    arr = pix.toImage().convertToFormat(QImage_Format_RGBA8888())
    assert arr.width() == 540
    p.shutdown()


def QImage_Format_RGBA8888():
    from PySide6.QtGui import QImage
    return QImage.Format_RGBA8888


def test_configure_media_backend_sets_env(monkeypatch):
    import platform as _pl
    from app.ui import player as pl
    monkeypatch.setattr(_pl, "system", lambda: "Darwin")
    monkeypatch.delenv("QT_MEDIA_BACKEND", raising=False)
    pl.configure_media_backend()
    assert os.environ["QT_MEDIA_BACKEND"] == "darwin"     # AVFoundation на Apple Silicon


# ------------------------------------------------ двусторонняя синхронизация
def _ui_with_subs(qapp):
    from app.ui.ui_main import MainWindow
    w = MainWindow()
    words = [Word(text=t, start=i * 1.0, end=i * 1.0 + 0.9)
             for i, t in enumerate(["раз", "два", "три", "четыре"])]
    w.project.phrases = [Phrase(words=words[:2]), Phrase(words=words[2:])]
    w.project.duration = 10.0
    w.scene = __import__("app.render.scene", fromlist=["SubtitleScene"]).SubtitleScene(
        w.project, 540, 960)
    w.player.set_scene(w.scene)
    w.table.load(w.project)
    return w


def test_playback_highlights_current_row(qapp):
    w = _ui_with_subs(qapp)
    w._on_position(0.5)
    assert w.table._active_row == 0
    w._on_position(2.5)
    assert w.table._active_row == 1
    w._on_position(9.5)
    assert w.table._active_row == -1          # вне субтитров — подсветки нет
    w.close()


def test_click_on_row_seeks_player(qapp):
    w = _ui_with_subs(qapp)
    w.table.selectRow(1)
    assert w.player.position() == pytest.approx(w.project.phrases[1].start, abs=0.01)
    w.close()


def test_live_edit_updates_preview_without_regeneration(qapp):
    w = _ui_with_subs(qapp)
    w._on_text_edited(0, "новый текст блока")
    assert w.project.phrases[0].text == "новый текст блока"
    items = w.scene.evaluate(w.project.phrases[0].start + 0.05)
    assert any(i.get("text") == "новый" for i in items)

    w._on_time_edited(1, 5.0, 6.0)             # сдвиг тайминга второй строки
    assert w.project.phrases[1].start == pytest.approx(5.0)
    assert w.scene.evaluate(5.1)               # рендерится уже на новом месте
    w._on_position(5.2)
    assert w.table._active_row == 1
    w.close()


# ============================================================================
# v3.0: фиксированный layout, физика, aspect-канвас, 15 пресетов
# ============================================================================
def test_physics_spring_and_easing_are_smooth():
    from app.render.animation_physics import (SpringParams, ease_out_cubic,
                                              spring_normalized, spring_scale)
    assert ease_out_cubic(0.0) == 0.0 and abs(ease_out_cubic(1.0) - 1.0) < 1e-9
    p = SpringParams()
    assert p.stiffness_k == 220 and p.mass_m == 1.0 and p.damping_c == 20
    assert p.zeta < 1.0                                  # недодемпфированная
    assert abs(spring_scale(0.0, p, start=0.4) - 0.4) < 1e-6
    assert abs(spring_scale(3.0, p) - 1.0) < 1e-3        # сходится к цели
    assert max(spring_normalized(t / 200.0, p) for t in range(200)) > 1.0  # overshoot
    prev = spring_scale(0.0, p, start=0.4)
    for i in range(1, 400):                              # без скачков
        cur = spring_scale(i / 400.0, p, start=0.4)
        assert abs(cur - prev) < 0.1
        prev = cur


def test_layout_is_computed_once_and_never_moves(qapp):
    from app.render.scene import SubtitleScene
    project = _demo_project()
    scene = SubtitleScene(project, 1080, 1920)
    layout = scene.layout_phrase(0)
    assert layout is scene.layout_phrase(0)              # кэш по фразе
    boxes = [w.rect for w in layout.words]
    for t in (layout.start, layout.start + 0.3, layout.end - 0.05, layout.end):
        scene.evaluate(t)
        assert [w.rect for w in scene.layout_phrase(0).words] == boxes
    scene.set_style(scene.project.global_style.model_copy(update={"font_size": 120}))
    assert [w.rect for w in scene.layout_phrase(0).words] != boxes   # стиль → пересчёт


def test_word_transform_only_changes_visuals(qapp):
    from app.render.layout_engine import apply_word_transform
    from app.render.scene import SubtitleScene
    scene = SubtitleScene(_demo_project(), 1080, 1920)
    layout = scene.layout_phrase(0)
    info = layout.words[0]
    pivot = info.pivot
    tr = apply_word_transform(info, info.word.start + 0.01, layout.style, layout.start)
    assert tr.scale != 1.0 or tr.opacity < 1.0
    assert info.pivot == pivot and info.rect == info.rect


def test_presets_catalog_has_15_unique_styles():
    from app.render.presets_catalog import PRESET_BY_KEY, PRESET_CATALOG, preset_categories
    assert len(PRESET_CATALOG) == 15
    assert PRESET_BY_KEY["capcut_yellow"].values["karaoke_color"] == "#FFD600"
    base = Style()
    for p in PRESET_CATALOG:
        s = p.apply(base)
        assert isinstance(s, Style) and s.animation in {
            "bounce", "karaoke", "fade", "static", "typewriter"}
    assert sum(len(v) for v in preset_categories().values()) == 15


def test_aspect_ratio_changes_real_resolution(qapp):
    from app.render.scene import SubtitleScene
    from app.ui.video_canvas import composition_size, source_rect, viewport_rect
    scene = SubtitleScene(_demo_project(), 1080, 1920)
    assert scene.use_aspect("16:9") == (1920, 1080)
    assert (scene.width, scene.height) == (1920, 1080)
    assert scene.use_aspect("1:1") == (1080, 1080)
    assert composition_size("4:3") == (1440, 1080)

    g = viewport_rect(800, 600, 1080, 1920)
    assert abs(g.viewport.height() - 600) < 1e-6 and g.viewport.width() < 800
    from PySide6.QtCore import QSize
    fit = source_rect(QSize(1920, 1080), 1080, 1920, "fit")
    assert fit.width() == 1080 and fit.height() < 1920
    fill = source_rect(QSize(1920, 1080), 1080, 1920, "fill")
    assert fill.height() >= 1920 - 1e-6


def test_canvas_renders_composition_with_background(qapp):
    from PySide6.QtGui import QColor, QImage
    from app.render.scene import SubtitleScene
    from app.ui.video_canvas import VideoCanvas
    canvas = VideoCanvas()
    canvas.resize(360, 640)
    canvas.set_scene(SubtitleScene(_demo_project(), 1080, 1920))
    frame = QImage(640, 360, QImage.Format_RGB888)
    frame.fill(QColor(30, 120, 200))
    canvas.set_frame(frame)
    canvas.set_fit_mode("fit_blur")
    img = canvas.render_composition(_demo_project().phrases[0].start + 0.2)
    assert (img.width(), img.height()) == (1080, 1920)
    assert img.pixelColor(20, 20).alpha() == 255          # поля закрашены
    canvas.set_aspect("16:9")
    assert canvas.render_composition(0.0).width() == 1920


def test_ui_aspect_and_fit_controls(qapp):
    from app.ui.ui_main import MainWindow
    w = MainWindow()
    w._on_aspect("16:9")
    assert (w.scene.width, w.scene.height) == (1920, 1080)
    assert w.project.global_style.safe_zone == "16:9"
    w._on_fit_mode("fill")
    assert w.project.global_style.fit_mode == "fill"
    assert len(w.style_grid._cards) == 15
    w._apply_style_preset("gradient_sunset")
    assert w.project.global_style.gradient_enabled
    w._apply_animation_preset("typewriter")
    assert w.project.global_style.animation == "typewriter"


# ============================================================================
# v3.1: никаких красных боксов — акцент управляет ЦВЕТОМ ТЕКСТА активного слова
# ============================================================================
def _scene_at(style, t=0.55):
    from app.render.scene import SubtitleScene
    pr = _demo_project()
    pr.global_style = style
    sc = SubtitleScene(pr, 1080, 1920)
    return sc, sc.evaluate(t)


def test_no_background_box_behind_active_word_by_default(qapp):
    from app.render.presets_catalog import PRESET_CATALOG
    for preset in PRESET_CATALOG:
        style = preset.apply(Style(smart_contrast=False))
        _, items = _scene_at(style, 0.5)
        boxes = [i for i in items if i["type"] == "active_card"]
        if preset.key == "submagic_pop":
            continue                       # единственный пресет с явной плашкой
        assert not boxes, f"{preset.key} рисует плашку под активным словом"
        for i in items:
            assert i["type"] != "active_card"


def test_accent_color_drives_active_word_text_color(qapp):
    style = Style(smart_contrast=False).with_accent("#FFFFFF")
    assert style.accent_color == "#FFFFFF"
    _, items = _scene_at(style, 0.5)
    words = [i for i in items if i["type"] == "word"]
    assert words and all(w["karaoke_color"] == "#FFFFFF" for w in words)
    style2 = style.with_accent("#FFEE00")
    _, items2 = _scene_at(style2, 0.5)
    assert all(w["karaoke_color"] == "#FFEE00"
               for w in items2 if w["type"] == "word")
    assert not [i for i in items2 if i["type"] == "active_card"]


def test_active_word_pops_with_scale_not_with_box(qapp):
    from app.render.layout_engine import apply_word_transform
    from app.render.scene import SubtitleScene
    pr = _demo_project()
    pr.global_style = Style(smart_contrast=False, animation="karaoke", active_scale=1.1)
    sc = SubtitleScene(pr, 1080, 1920)
    layout = sc.layout_phrase(0)
    info = layout.words[1]
    mid = (info.word.start + info.word.end) / 2.0
    active = apply_word_transform(info, mid, layout.style, layout.start)
    idle = apply_word_transform(info, layout.start, layout.style, layout.start)
    assert active.scale > idle.scale and active.scale <= 1.11


def test_translucent_backing_is_one_rect_per_line(qapp):
    style = Style(smart_contrast=False, backing_enabled=True, backing_opacity=0.6)
    on, color, radius = style.line_card
    assert on and color == "#00000099" and radius == 8      # rgba(0,0,0,0.6), r=8
    _, items = _scene_at(style, 0.5)
    cards = [i for i in items if i["type"] == "card"]
    assert len(cards) == 1                                   # одна подложка на строку
    words = [i for i in items if i["type"] == "word"]
    assert cards[0]["w"] >= max(w["advance"] for w in words)
    assert not [i for i in items if i["type"] == "active_card"]


def test_switching_preset_clears_word_box(qapp):
    from app.render.presets_catalog import PRESET_BY_KEY
    s = PRESET_BY_KEY["submagic_pop"].apply(Style())
    assert s.active_card_enabled
    s = PRESET_BY_KEY["glassmorphism"].apply(s)
    assert not s.active_card_enabled                        # бокс не «прилипает»


def test_descript_minimal_dims_inactive_words(qapp):
    from app.render.presets_catalog import PRESET_BY_KEY
    style = PRESET_BY_KEY["descript_minimal"].apply(Style(smart_contrast=False))
    assert style.inactive_opacity == 0.5
    _, items = _scene_at(style, 0.5)
    words = [i for i in items if i["type"] == "word"]
    act = [w for w in words if w["active"]]
    inact = [w for w in words if not w["active"]]
    assert act and inact
    assert min(w["opacity"] for w in act) > max(w["opacity"] for w in inact)
    assert not [i for i in items if i["type"] == "active_card"]


def test_ui_color_panel_is_source_of_truth(qapp):
    from app.ui.ui_main import MainWindow
    w = MainWindow()
    w._apply_style_preset("submagic_pop")
    assert w.project.global_style.active_card_enabled
    w.chk_word_box.setChecked(False)
    assert not w.project.global_style.active_card_enabled
    w.set_color("accent", "#FFFFFF")
    st = w.project.global_style
    assert st.accent_color == "#FFFFFF" and st.karaoke_color == "#FFFFFF"
    w.set_color("fill_color", "#DDDDDD")
    assert w.project.global_style.fill_color == "#DDDDDD"
    w._on_backing_toggled(True)
    on, color, radius = w.project.global_style.line_card
    assert on and color == "#00000099" and radius == 8


# ============================================================================
# v3.2: UI-полировка, новые анимации, мгновенная перерисовка предпросмотра
# ============================================================================
def test_combobox_style_has_visible_arrow():
    from app.ui.theme import QSS
    from app.ui.theme import CHEVRON
    import os
    assert "QComboBox::down-arrow" in QSS and "chevron_down.svg" in QSS
    assert os.path.exists(CHEVRON)                     # стрелка ▼ реально есть
    assert "QComboBox:hover" in QSS and "QComboBox::drop-down" in QSS
    assert "QComboBox QAbstractItemView" in QSS


def test_font_picker_looks_clickable(qapp):
    from app.ui.ui_main import MainWindow
    w = MainWindow()
    assert w.font_box.toolTip()
    assert w.font_box.minimumHeight() >= 30
    assert w.font_box.objectName() == "FontPicker"


def test_word_box_checkbox_gates_color_button(qapp):
    from app.ui.ui_main import MainWindow
    w = MainWindow()
    assert not w.btn_word_box.isEnabled()
    assert "Submagic" in w.chk_word_box.toolTip()
    w.chk_word_box.setChecked(True)
    assert w.btn_word_box.isEnabled() and w.project.global_style.active_card_enabled
    w.chk_word_box.setChecked(False)
    assert not w.btn_word_box.isEnabled()


def test_inactive_opacity_label_and_tooltip(qapp):
    from app.ui.ui_main import MainWindow
    w = MainWindow()
    assert "караоке" in w.sld_inactive.toolTip()
    w.sld_inactive.setValue(40)
    assert w.lbl_inactive.text() == "40%"
    assert abs(w.project.global_style.inactive_opacity - 0.4) < 1e-9


def test_fit_mode_and_aspect_refresh_preview_immediately(qapp):
    from app.ui.ui_main import MainWindow
    w = MainWindow()
    calls = []
    w.update_preview_frame = lambda: calls.append(1)     # type: ignore[method-assign]
    w.seg_fit.set_value("fill")
    w._on_fit_mode("fill")
    w._on_aspect("16:9")
    assert len(calls) >= 2
    assert w.project.global_style.fit_mode == "fill"
    w.update_preview_frame = MainWindow.update_preview_frame.__get__(w)
    w.update_preview_frame()                              # реальный вызов не падает


def test_advanced_physics_tooltips(qapp):
    from app.ui.ui_main import MainWindow
    w = MainWindow()
    assert "жёстк" in w.spin_k.toolTip().lower()
    assert "масса" in w.spin_m.toolTip().lower()
    assert "ζ" in w.spin_c.toolTip() or "отскок" in w.spin_c.toolTip()
    assert "бочк" in w.spin_pulse.toolTip().lower() or "kick" in w.spin_pulse.toolTip()


def test_eleven_animation_presets_render(qapp):
    from app.render.scene import SubtitleScene
    from app.render.style_presets import ANIMATION_BY_KEY, ANIMATION_PRESETS
    assert len(ANIMATION_PRESETS) == 11
    for key in ("pop_in", "slide_up", "glow_pulse", "fade_zoom", "wave_bounce",
                "glitch_flash"):
        assert key in ANIMATION_BY_KEY
        pr = _demo_project()
        pr.global_style = ANIMATION_BY_KEY[key].apply(
            Style(smart_contrast=False, font_size=72))
        sc = SubtitleScene(pr, 540, 960)
        words = [i for i in sc.evaluate(pr.phrases[0].start + 0.3)
                 if i["type"] == "word"]
        assert words, key
        img = sc.render_frame(pr.phrases[0].start + 0.3)
        assert not img.isNull()


def test_new_animations_move_but_keep_layout(qapp):
    from app.render.layout_engine import apply_word_transform
    from app.render.scene import SubtitleScene
    from app.render.style_presets import ANIMATION_BY_KEY
    for key, check in (("slide_up", "offset_y"), ("wave_bounce", "offset_y"),
                       ("pop_in", "scale"), ("fade_zoom", "scale"),
                       ("glitch_flash", "chroma_shift"), ("glow_pulse", "glow")):
        pr = _demo_project()
        pr.global_style = ANIMATION_BY_KEY[key].apply(Style(smart_contrast=False))
        sc = SubtitleScene(pr, 1080, 1920)
        layout = sc.layout_phrase(0)
        info = layout.words[0]
        rect = info.rect
        values = []
        for i in range(12):
            t = layout.start + 0.02 * i + (0.0 if key != "glow_pulse" else 0.0)
            values.append(getattr(apply_word_transform(info, t, layout.style,
                                                       layout.start), check))
        assert info.rect == rect                       # layout не двигается
        if key == "glow_pulse":
            mid = (info.word.start + info.word.end) / 2
            assert apply_word_transform(info, mid, layout.style, layout.start).glow > 0
        else:
            assert max(values) != min(values), key
