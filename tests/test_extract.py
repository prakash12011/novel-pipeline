import zipfile

import pytest
import yaml

from novel.commands import extract_cmd
from novel.stages import extract as ex

WORDS = "alpha beta gamma delta epsilon zeta eta theta iota kappa " * 25   # 250 words


def page(title, paras=None, h=None):
    paras = paras if paras is not None else [WORDS]
    heading = f"<h2>{h}</h2>" if h else ""
    body = "".join(f"<p>{p}</p>" for p in paras)
    return (f'<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml"><head>'
            f"<title>{title}</title></head><body>{heading}<div>{body}</div></body></html>")


def make_epub(path, docs, spine=None):
    """docs: [(zip name, xhtml)] in zip order; spine: list of names (default zip order)."""
    spine = spine if spine is not None else [n for n, _ in docs]
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml",
                   '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
                   '<rootfile full-path="OEBPS/book.opf"/></rootfiles></container>')
        items = "".join(f'<item id="i{i}" href="{n}" media-type="application/xhtml+xml"/>'
                        for i, (n, _) in enumerate(docs))
        ids = {n: f"i{i}" for i, (n, _) in enumerate(docs)}
        refs = "".join(f'<itemref idref="{ids[n]}"/>' for n in spine)
        z.writestr("OEBPS/book.opf",
                   f'<package xmlns="http://www.idpf.org/2007/opf"><manifest>{items}</manifest>'
                   f"<spine>{refs}</spine></package>")
        for n, content in docs:
            z.writestr(f"OEBPS/{n}", content)


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    data = tmp_path / "data"
    home.mkdir()
    data.mkdir()
    monkeypatch.setenv("NOVEL_HOME", str(home))
    (home / "settings.yaml").write_text(yaml.safe_dump({"data_dir": str(data)}), encoding="utf-8")
    book = data / "b"
    (book / "source").mkdir(parents=True)
    return book


def run(*args):
    return extract_cmd.main(["--book", "b", *args])


def names(d):
    return sorted(p.name for p in d.glob("*.txt"))


def test_spine_order_beats_zip_order(tmp_path):
    p = tmp_path / "a.epub"
    make_epub(p, [("z.xhtml", page("Chapter 2: Two")), ("a.xhtml", page("Chapter 1: One")),
                  ("m.xhtml", page("Chapter 3: Three"))],
              spine=["a.xhtml", "z.xhtml", "m.xhtml"])
    sc = ex.scan(tmp_path)
    assert sc.numbers == [1, 2, 3]
    assert [c.title for c in sc.chapters] == ["One", "Two", "Three"]


def test_non_chapters_skipped_and_counted(tmp_path):
    make_epub(tmp_path / "a.epub", [("cover.xhtml", page("Cover")),
                                    ("toc.xhtml", page("Contents")),
                                    ("c1.xhtml", page("Chapter 1 - First")),
                                    ("c2.xhtml", page("x", h="Chapter 2")),
                                    ("notes.xhtml", page("Author notes"))])
    sc = ex.scan(tmp_path)
    assert sc.numbers == [1, 2]
    assert sc.skipped == 3
    assert sc.chapters[0].title == "First"
    assert sc.chapters[1].title == ""


def test_two_epubs_ordered_by_first_chapter(tmp_path):
    make_epub(tmp_path / "a-late.epub", [("c.xhtml", page("Chapter 3: C")), ("d.xhtml", page("Chapter 4: D"))])
    make_epub(tmp_path / "z-early.epub", [("a.xhtml", page("Chapter 1: A")), ("b.xhtml", page("Chapter 2: B"))])
    sc = ex.scan(tmp_path)
    assert sc.epubs == ["z-early.epub", "a-late.epub"]
    assert sc.numbers == [1, 2, 3, 4]


def test_gaps_and_duplicates(tmp_path):
    make_epub(tmp_path / "a.epub", [("1.xhtml", page("Chapter 1: A")), ("2.xhtml", page("Chapter 2: B")),
                                    ("5.xhtml", page("Chapter 5: E")), ("6.xhtml", page("Chapter 7: G")),
                                    ("7.xhtml", page("Chapter 5: E again"))])
    sc = ex.scan(tmp_path)
    assert sc.gaps == [(3, 4), (6, 6)]
    assert sc.duplicates == [5]
    assert sc.numbers == [1, 2, 5, 7]


def test_regex_override_from_settings(env):
    make_epub(env / "source" / "a.epub", [("1.xhtml", page("Part 1 | Dawn")), ("2.xhtml", page("Part 2 | Dusk")),
                                          ("c.xhtml", page("Chapter 9: ignored"))])
    (env / "book.yaml").write_text(
        yaml.safe_dump({"book": {"chapter_regex": r"^Part (?P<num>\d+) \| (?P<title>.+)$"}}), encoding="utf-8")
    assert run() == 0
    assert names(env / "chapters") == ["0001_dawn.txt", "0002_dusk.txt"]


def test_volumes_from_settings(env, capsys):
    make_epub(env / "source" / "a.epub", [(f"{i}.xhtml", page(f"Chapter {i}: T{i}")) for i in (1, 2, 3)])
    (env / "book.yaml").write_text(
        yaml.safe_dump({"book": {"volumes": [[1, 2, 1], [3, 4, 2]]}}), encoding="utf-8")
    assert run("--check") == 0
    assert "volumes: 1: 2 chapters, 2: 1 chapters" in capsys.readouterr().out
    sc = ex.scan(env / "source", volumes=[[1, 2, 1], [3, 4, 2]])
    assert [c.volume for c in sc.chapters] == [1, 1, 2]
    assert ex.scan(env / "source").chapters[0].volume == 1


def test_mojibake_fix_and_text_shape(env):
    broken = "DariÃ¨ge and CÅ“ur"
    make_epub(env / "source" / "a.epub",
              [("1.xhtml", page("Chapter 1: DariÃ¨ge &amp; Co", [broken, WORDS]))])
    assert run() == 0
    f = next((env / "chapters").glob("*.txt"))
    assert f.name == "0001_dari-ge-co.txt" or f.name.startswith("0001_")
    text = f.read_text(encoding="utf-8")
    assert text.startswith("Chapter 1: Dariège & Co\n\nDariège and Cœur\n\n")
    assert text.endswith("\n") and not text.endswith("\n\n")


def test_check_writes_nothing_and_flags_problems(env, capsys):
    make_epub(env / "source" / "a.epub", [("1.xhtml", page("Chapter 1: A")),
                                          ("3.xhtml", page("Chapter 3: C", ["tiny"]))])
    assert run("--check") == 1
    out = capsys.readouterr().out
    assert "gaps: 2" in out and "suspiciously short" in out
    assert not (env / "chapters").exists()


def test_check_ok_exit_zero(env):
    make_epub(env / "source" / "a.epub", [("1.xhtml", page("Chapter 1: A")), ("2.xhtml", page("Chapter 2: B"))])
    assert run("--check") == 0
    assert not (env / "chapters").exists()


def test_refuses_to_overwrite_changed_files(env, capsys):
    make_epub(env / "source" / "a.epub", [("1.xhtml", page("Chapter 1: A")), ("2.xhtml", page("Chapter 2: B"))])
    assert run() == 0
    assert run() == 0                       # identical content: fine
    f = env / "chapters" / "0001_a.txt"
    f.write_text("edited by hand\n", encoding="utf-8")
    assert run() == 1
    assert "0001_a.txt" in capsys.readouterr().err
    assert f.read_text(encoding="utf-8") == "edited by hand\n"
    assert run("--force") == 0
    assert f.read_text(encoding="utf-8").startswith("Chapter 1: A")


def test_no_epub_says_where_to_put_it(env, capsys):
    assert run() == 1
    assert str(env / "source") in capsys.readouterr().err


def test_text_file_source(env):
    (env / "source" / "book.txt").write_text(
        "Preface text ignored\n\nChapter 1: Start\n\nFirst line\nwraps here.\n\nSecond para.\n\n"
        "Chapter 2 - Next\n\nBody two.\n", encoding="utf-8")
    assert run() == 0
    one = (env / "chapters" / "0001_start.txt").read_text(encoding="utf-8")
    assert one == "Chapter 1: Start\n\nFirst line wraps here.\n\nSecond para.\n"
    assert (env / "chapters" / "0002_next.txt").exists()


def test_extract_function_keeps_old_shape(tmp_path):
    make_epub(tmp_path / "a.epub", [(f"{i}.xhtml", page(f"Chapter {i}: T{i}")) for i in (1, 2, 3)])
    report = ex.extract(tmp_path, tmp_path / "out", 2, 3)
    assert report == [(2, "T2", 250), (3, "T3", 250)]
    assert names(tmp_path / "out") == ["0002_t2.txt", "0003_t3.txt"]
