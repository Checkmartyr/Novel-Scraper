import pytest
from src.utils.romanizer import romanize_text, is_japanese
from src.scraper.storage import NovelStorage

def test_is_japanese():
    assert is_japanese("悪役貴族による弱小領地の革命開拓") is True
    assert is_japanese("カクヨム") is True
    assert is_japanese("诡秘之主") is False
    assert is_japanese("나 혼자만 레벨업") is False
    assert is_japanese("English Novel") is False

def test_romanize_japanese():
    title = "悪役貴族による弱小領地の革命開拓"
    romanized = romanize_text(title)
    assert "Akuyaku" in romanized
    assert "Kizoku" in romanized
    assert "Kaitaku" in romanized

def test_romanize_chinese():
    # Lord of the Mysteries in Chinese
    title = "诡秘之主"
    romanized = romanize_text(title)
    assert romanized == "GuiMiZhiZhu"

def test_romanize_korean():
    # Solo Leveling in Korean
    title = "나 혼자만 레벨업"
    romanized = romanize_text(title)
    assert "Na" in romanized
    assert "LeBelEob" in romanized

def test_romanize_russian():
    # War and Peace in Russian
    title = "Война и мир"
    romanized = romanize_text(title)
    assert "Voyna" in romanized
    assert "mir" in romanized

def test_romanize_english():
    title = "Lord of the Mysteries"
    assert romanize_text(title) == "Lord of the Mysteries"

def test_storage_romanized_dir(tmp_path):
    storage_romanized = NovelStorage(base_output_dir=tmp_path, romanize_folder=True)
    dir_romanized = storage_romanized.get_novel_dir("悪役貴族による弱小領地の革命開拓")
    assert "Akuyaku Kizoku" in dir_romanized.name

    storage_raw = NovelStorage(base_output_dir=tmp_path, romanize_folder=False)
    dir_raw = storage_raw.get_novel_dir("悪役貴族による弱小領地の革命開拓")
    assert dir_raw.name == "悪役貴族による弱小領地の革命開拓"
