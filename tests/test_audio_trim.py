"""Тесты потери первых слов + поведение на битых/пустых/тихих файлах.

Регрессия бага: «Программа пропускает первые 1-2 слова песни в начале трека
(аудио обрезается или VAD отбрасывает вокал как шум)».

Что проверяем:
  A. head_trim_point — отступ НЕ МЕНЬШЕ 0.5–1 с ПЕРЕД первой вспышкой вокала;
  B. энергетический VAD видит тихую атаку (fade-in) первого слова;
  C. postprocess / filter_by_vad / drop_intro_hallucinations берегут ранние слова,
     при этом интро-галлюцинации («Песня «Музыка»») по-прежнему удаляются;
  D. align_text режет аудио с пре-роллом, а не по самой вспышке (FakeAligner
     захватывает длительность полученного PCM);
  E. битые файлы, абсолютная тишина и пустые WAV не роняют интерпретатор;
  F. ручная задержка субтитров (subtitle_offset) — слайдер, сцена и экспорт SRT.
"""
from __future__ import annotations

import os

import numpy as np
import pytest

SR = 16000


def _tone(seconds: float, freq: float = 220.0, amp: float = 0.5,
          fade_in: float = 0.0, sr: int = SR) -> np.ndarray:
    t = np.arange(int(seconds * sr)) / sr
    env = np.ones_like(t)
    if fade_in > 0.0:
        n = int(fade_in * sr)
        env[:n] = np.linspace(0.12, 1.0, n)
    return (amp * env * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def _silence(seconds: float, sr: int = SR) -> np.ndarray:
    return np.zeros(int(seconds * sr), dtype=np.float32)


# ---------------------------------------------------------------- A. trim math
def test_head_trim_point_defaults_one_second():
    from app.core.asr_engine import head_trim_point
    assert head_trim_point(10.0) == pytest.approx(9.0)     # 1.0 с пре-ролла
    assert head_trim_point(10.0, head_padding=0.5) == pytest.approx(9.5)
    assert head_trim_point(2.0, head_padding=1.0) == pytest.approx(1.0)
    assert head_trim_point(0.3) == 0.0                     # клампится в ноль
    assert head_trim_point(0.0) == 0.0


def test_head_trim_point_survives_garbage_input():
    from app.core.asr_engine import head_trim_point
    assert head_trim_point(float("nan")) == 0.0
    assert head_trim_point(-5.0) == 0.0
    assert head_trim_point(10.0, head_padding=-1.0) == pytest.approx(10.0)


def test_head_padding_is_plumbed_into_engine_and_options():
    from app.audio.audio_processor import AudioProcessor, ProcessOptions
    from app.core.asr_engine import HEAD_PADDING_SECONDS, TranscriptionEngine
    assert ProcessOptions().head_padding == pytest.approx(HEAD_PADDING_SECONDS)
    assert HEAD_PADDING_SECONDS >= 0.5                      # требование ТЗ: ≥ 0.5 с
    engine = TranscriptionEngine("small")
    assert engine.head_padding == pytest.approx(HEAD_PADDING_SECONDS)
    custom = AudioProcessor(ProcessOptions(head_padding=2.0))
    assert custom.engine.head_padding == pytest.approx(2.0)


# ----------------------------------------------------- B. VAD слышит fade-in
def test_energy_vad_detects_quiet_fade_in_attack():
    """Очень тихое начало вокала на фоне почти тишины → onset ≈ началу fade-in."""
    from app.core.asr_engine import detect_voice_activity
    pcm = np.concatenate([
        0.001 * np.random.RandomState(7).randn(SR * 6).astype(np.float32),   # «интро»
        _tone(4.0, amp=0.7, fade_in=1.5),                                    # fade-in голоса
    ])
    va = detect_voice_activity(pcm, use_silero=False)
    assert va.segments, "VAD обязан найти вокал"
    # пре-ролл 0.3 с из energy_vad_segments: onset не позже начала громкой части,
    # тихая атака попадает внутрь первого сегмента
    assert va.first_onset <= 6.5


def test_energy_vad_absolute_silence_returns_nothing():
    from app.core.asr_engine import detect_voice_activity, energy_vad_segments
    zeros = _silence(3.0)
    assert energy_vad_segments(zeros) == []
    va = detect_voice_activity(zeros, use_silero=False)
    assert va.segments == [] and va.first_onset == 0.0     # без падения и без сегментов


# -------------------------------------- C. ранние слова не выкидываются фильтрами
def _w(text, a, b):
    from app.core.models import Word
    return Word(text=text, start=a, end=b)


def test_filter_by_vad_head_padding_keeps_fadein_word():
    from app.core.asr_engine import filter_by_vad
    words = [_w("Я", 0.2, 0.9), _w("иду", 0.9, 1.4)]
    assert filter_by_vad(words, [(2.0, 6.0)]) == []          # прежнее поведение
    kept = filter_by_vad(words, [(2.0, 6.0)], head_padding=1.5)
    assert [w.text for w in kept] == ["Я", "иду"]


def test_filter_by_vad_first_word_mode_only_affects_first_segment():
    from app.core.asr_engine import filter_by_vad
    words = [_w("буфер", 0.1, 0.9), _w("речь", 10.5, 11.0)]
    kept = filter_by_vad(words, [(2.0, 4.0), (10.5, 12.0)], head_padding=1.5)
    # пре-ролл применён только к ПЕРВОМУ интервалу; слова из середины интро
    # (между сегментами) по-прежнему отбрасываются
    assert [w.text for w in kept] == ["буфер", "речь"]
    kept = filter_by_vad(words, [(2.0, 4.0), (10.5, 12.0)], head_padding=0.0)
    assert [w.text for w in kept] == ["речь"]


def test_drop_intro_hallucinations_head_padding_keeps_real_words():
    from app.core.asr_engine import drop_intro_hallucinations
    words = [_w("Я", 0.1, 0.7), _w("иду", 0.7, 1.2)]
    old = drop_intro_hallucinations(words, first_onset=1.5)                # старое поведение
    assert [w.text for w in old] == ["иду"]   # «Я» удалено целиком, «иду» подрезано к лимиту
    kept = drop_intro_hallucinations(words, first_onset=1.5, head_padding=1.0)
    assert [w.text for w in kept] == ["Я", "иду"]
    # а вот «Песня «Музыка»» сквозь пре-ролл не проходит: end целиком в интро
    hallucinated = [_w("Песня", 0.2, 1.2), _w("«Музыка»", 1.3, 2.6)]
    assert drop_intro_hallucinations(hallucinated, first_onset=3.0,
                                     head_padding=1.0) == []


def test_intro_anchor_does_not_shift_whole_track_insanely():
    """Сдвиг >0.75 с (поздний VAD) больше не «перетаскивает» всю песню."""
    from app.core.asr_engine import drop_intro_hallucinations
    words = [_w("реальное", 5.0, 5.4), _w("слово", 5.4, 5.8)]
    kept = drop_intro_hallucinations(words, first_onset=8.0, head_padding=3.0)
    assert [w.text for w in kept] == ["реальное", "слово"]
    assert kept[0].start == pytest.approx(5.0)               # тайминг не дёргался
    # умеренный сдвиг по-прежнему якорится к онсету
    words = [_w("первое", 8.2, 8.6), _w("слово", 8.6, 9.0)]
    kept = drop_intro_hallucinations(words, first_onset=8.0, head_padding=0.0)
    assert kept[0].start == pytest.approx(8.0, abs=0.01)


def test_postprocess_keeps_fadein_first_words_but_drops_intro_hallucination():
    from app.core.asr_engine import TranscriptionEngine, VoiceActivity
    eng = TranscriptionEngine("small")
    va = VoiceActivity(first_onset=1.5, segments=[(1.5, 8.0)], method="energy")
    words = [_w("Я", 0.3, 0.8), _w("иду", 0.8, 1.2),
             _w("по", 1.6, 1.9), _w("городу", 1.9, 2.3)]
    out = eng.postprocess(words, va)
    assert [w.text for w in out][:2] == ["Я", "иду"]          # первые слова живы
    assert [w.text for w in out] == ["Я", "иду", "по", "городу"]

    words = [_w("Песня", 0.0, 18.0), _w("«Музыка»", 18.0, 30.0),
             _w("Первая", 30.1, 30.7), _w("строчка", 30.7, 31.1)]
    va = VoiceActivity(first_onset=30.0, segments=[(30.0, 60.0)], method="energy")
    out = eng.postprocess(words, va)
    assert [w.text for w in out] == ["Первая", "строчка"]


# ------------------------------------------ D. align_text: пре-ролл в аудио
def test_align_text_trims_with_one_second_head_padding(tmp_path, monkeypatch):
    """FakeAligner захватывает длительность PCM: она обязана быть МАКСИМАЛЬНО раньше
    первой вспышки вокала — иначе первые слова физически отрезаны до выравнивания."""
    from app.audio import extract
    from app.core import asr_engine
    from app.core.asr_engine import TranscriptionEngine

    pcm = np.concatenate([_silence(6.0), _tone(4.0, amp=0.7, fade_in=1.0)])
    path = extract.write_wav(tmp_path / "vocals.wav", pcm, SR)

    seen = {}

    class FakeAligner:
        def available(self):
            return True

        def align(self, pcm16k, seed):
            seen["seconds"] = pcm16k.size / SR
            return [w.model_copy() for w in seed]

    monkeypatch.setattr(TranscriptionEngine, "stable_ts_available", staticmethod(lambda: False))
    monkeypatch.setattr(asr_engine, "MMSForcedAligner", FakeAligner)

    res = TranscriptionEngine("small").align_text(path, "первая строка песни")

    va_first = res.first_onset
    total = pcm.size / SR                       # 10.0 с
    # без пре-ролла было onset−0.08 → доступно total − (onset−0.08);
    # с пре-роллом 1.0 с доступно минимум на ~0.9 с больше аудио ПЕРЕД вспышкой
    assert seen["seconds"] >= total - (va_first - 1.0) - 0.05
    assert seen["seconds"] > total - va_first + 0.9
    # жёсткая привязка первого слова к первой секунде голоса сохранена
    assert res.words and abs(res.words[0].start - va_first) < 0.1
    assert [w.text for w in res.words] == ["первая", "строка", "песни"]


def test_align_text_fade_in_from_track_start_is_not_cut(tmp_path, monkeypatch):
    """Голос с самой первой секунды (fade-in с 0 с): резать нечего — берём всё."""
    from app.audio import extract
    from app.core import asr_engine
    from app.core.asr_engine import TranscriptionEngine

    pcm = _tone(6.0, amp=0.7, fade_in=2.0)      # тихий fade-in прямо с нуля
    path = extract.write_wav(tmp_path / "vocals.wav", pcm, SR)

    seen = {}

    class FakeAligner:
        def available(self):
            return True

        def align(self, pcm16k, seed):
            seen["seconds"] = pcm16k.size / SR
            return [w.model_copy() for w in seed]

    monkeypatch.setattr(TranscriptionEngine, "stable_ts_available", staticmethod(lambda: False))
    monkeypatch.setattr(asr_engine, "MMSForcedAligner", FakeAligner)

    res = TranscriptionEngine("small").align_text(path, "начало песни")
    assert seen["seconds"] >= 5.0               # практически весь трек ушёл в выравнивание
    assert res.words[0].start < 1.5             # первое слово НЕ унесено к VAD-онсету


# ------------------------------------------ E. битые файлы и чистая тишина
def test_decode_corrupt_file_raises_actionable_error(tmp_path):
    from app.audio import extract
    bad = tmp_path / "broken.wav"
    bad.write_bytes(os.urandom(4096))           # не WAV вообще
    with pytest.raises(extract.AudioDecodeError):
        extract.decode_pcm(str(bad), sr=SR)


def test_decode_truncated_header_raises(tmp_path):
    from app.audio import extract
    bad = tmp_path / "truncated.wav"
    bad.write_bytes(b"RIFF\x24\x00\x00\x00WAVE")  # оборванный заголовок
    with pytest.raises(extract.AudioDecodeError):
        extract.decode_pcm(str(bad), sr=SR)


def test_decode_empty_wav_raises(tmp_path):
    from app.audio import extract
    empty = extract.write_wav(tmp_path / "empty.wav", np.zeros(0, dtype=np.float32), SR)
    with pytest.raises(extract.AudioDecodeError):
        extract.decode_pcm(empty, sr=SR)


def test_missing_file_raises_decode_error(tmp_path):
    from app.audio import extract
    with pytest.raises(extract.AudioDecodeError):
        extract.decode_pcm(str(tmp_path / "nope.wav"), sr=SR)


def test_absolute_silence_end_to_end(tmp_path):
    """Файл из честных нулей: пайплайн не падает, слов нет, VAD пуст."""
    from app.audio import extract
    from app.core.asr_engine import TranscriptionEngine, detect_voice_activity

    path = extract.write_wav(tmp_path / "silence.wav", _silence(2.0), SR)
    pcm = extract.decode_pcm(path, sr=SR)
    assert pcm.dtype == np.float32 and pcm.size == SR * 2

    va = detect_voice_activity(pcm, use_silero=False)
    assert va.segments == [] and va.first_onset == 0.0

    from app.core.asr_engine import VoiceActivity
    assert TranscriptionEngine("small").postprocess(
        [], VoiceActivity(first_onset=0.0, segments=[])) == []


# ------------------------------------------ F. ручная задержка субтитров
def test_project_roundtrip_keeps_subtitle_offset(tmp_path):
    from app.core.models import Project
    p = Project(video_path="v.mp4", duration=30.0, subtitle_offset=0.35)
    f = tmp_path / "p.ctp"
    p.save(f)
    assert Project.load(f).subtitle_offset == pytest.approx(0.35)
    assert Project().subtitle_offset == 0.0           # дефолт = прежнее поведение
    # старые проекты без поля читаются: subtitle_offset заполняется нулём
    legacy = Project.model_validate({"video_path": "old.mp4", "phrases": []})
    assert legacy.subtitle_offset == 0.0


def test_scene_evaluate_applies_subtitle_offset():
    from app.core.models import Phrase, Project, Word
    from app.render.scene import SubtitleScene

    words = [Word(text="раз", start=1.0, end=1.9)]
    proj = Project(video_path="", duration=10.0, width=540, height=960,
                   phrases=[Phrase(words=words)], subtitle_offset=0.5)
    scene = SubtitleScene(proj, 540, 960)
    assert scene.visible_phrases(1.0) == []           # в медиа-времени рано
    assert scene.visible_phrases(1.6) == [0]          # субтитёр сдвинулся на +0.5 с

    proj0 = Project(video_path="", duration=10.0, width=540, height=960,
                    phrases=[Phrase(words=[Word(text="раз", start=1.0, end=1.9)])],
                    subtitle_offset=-0.5)
    scene0 = SubtitleScene(proj0, 540, 960)
    assert scene0.visible_phrases(0.6) == [0]         # показывают РАНЬШЕ

    proj_zero = Project(video_path="", duration=10.0, width=540, height=960,
                        phrases=[Phrase(words=[Word(text="раз", start=1.0, end=1.9)])])
    assert SubtitleScene(proj_zero, 540, 960).visible_phrases(1.0) == [0]


def test_srt_export_applies_subtitle_offset(tmp_path):
    from app.core.models import Phrase, Project, Word
    from app.render.exporter import VideoExporter

    proj = Project(video_path="", duration=10.0, subtitle_offset=0.5,
                   phrases=[Phrase(words=[Word(text="привет", start=1.0, end=2.0)])])
    out = VideoExporter.export_srt(proj, str(tmp_path / "o.srt"))
    text = open(out, encoding="utf-8").read()
    assert "00:00:01,500 --> 00:00:02,500" in text

    proj_neg = Project(video_path="", duration=10.0, subtitle_offset=-3.0,
                       phrases=[Phrase(words=[Word(text="привет", start=1.0, end=2.0)])])
    out2 = VideoExporter.export_srt(proj_neg, str(tmp_path / "o2.srt"))
    text2 = open(out2, encoding="utf-8").read()
    assert "00:00:00,000 --> 00:00:00,010" in text2   # кламп в ноль, без отрицательных таймстампов


def test_ui_offset_slider_updates_project_and_label(qapp):
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from app.ui.ui_main import MainWindow
    w = MainWindow()
    assert w.sld_delay.value() == 0
    w.sld_delay.setValue(10)                           # +0.50 с
    assert w.project.subtitle_offset == pytest.approx(0.5)
    assert w.lbl_delay.text() == "+0.50 с"
    w.sld_delay.setValue(-6)                           # −0.30 с
    assert w.project.subtitle_offset == pytest.approx(-0.3)
    assert w.lbl_delay.text() == "-0.30 с"
    w.btn_delay_reset.click()
    assert w.project.subtitle_offset == 0.0
    assert w.lbl_delay.text() == "+0.00 с"
    w.close()
