"""novel prompt --chapters A-B [--book B] [--mode auto|full|scenes] [--copy] [--out DIR]"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002


def set_clipboard(text: str) -> bool:
    """Put text on the Windows clipboard as UTF-16 (CF_UNICODETEXT). False when unavailable."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        k32, u32 = ctypes.windll.kernel32, ctypes.windll.user32
        k32.GlobalAlloc.restype = wintypes.HGLOBAL
        k32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
        k32.GlobalLock.restype = ctypes.c_void_p
        k32.GlobalLock.argtypes = [wintypes.HGLOBAL]
        k32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        k32.GlobalFree.argtypes = [wintypes.HGLOBAL]
        u32.OpenClipboard.argtypes = [wintypes.HWND]
        u32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
        data = (text + "\0").encode("utf-16-le")
        opened = False
        for _ in range(20):                       # another program may hold the clipboard for a moment
            if u32.OpenClipboard(None):
                opened = True
                break
            import time
            time.sleep(0.05)
        if not opened:
            return False
        try:
            u32.EmptyClipboard()
            h = k32.GlobalAlloc(GMEM_MOVEABLE, len(data))
            if not h:
                return False
            p = k32.GlobalLock(h)
            if not p:
                k32.GlobalFree(h)
                return False
            ctypes.memmove(p, data, len(data))
            k32.GlobalUnlock(h)
            if not u32.SetClipboardData(CF_UNICODETEXT, h):
                k32.GlobalFree(h)
                return False
            return True                            # the clipboard now owns the memory
        finally:
            u32.CloseClipboard()
    except Exception:
        return False


def get_clipboard() -> str | None:
    """The clipboard's text (CF_UNICODETEXT), or None when there is none or it is unavailable."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        k32, u32 = ctypes.windll.kernel32, ctypes.windll.user32
        k32.GlobalLock.restype = ctypes.c_void_p
        k32.GlobalLock.argtypes = [wintypes.HGLOBAL]
        k32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        u32.OpenClipboard.argtypes = [wintypes.HWND]
        u32.GetClipboardData.restype = wintypes.HANDLE
        u32.GetClipboardData.argtypes = [wintypes.UINT]
        opened = False
        for _ in range(20):
            if u32.OpenClipboard(None):
                opened = True
                break
            import time
            time.sleep(0.05)
        if not opened:
            return None
        try:
            h = u32.GetClipboardData(CF_UNICODETEXT)
            if not h:
                return None
            p = k32.GlobalLock(h)
            if not p:
                return None
            try:
                return ctypes.wstring_at(p)
            finally:
                k32.GlobalUnlock(h)
        finally:
            u32.CloseClipboard()
    except Exception:
        return None


def main(argv) -> int:
    from novel.core import context
    from novel.gemini import prompt as P

    ap = argparse.ArgumentParser(prog="novel prompt",
                                 description="Write the Gemini prompt and the files to upload for a chapter batch.")
    ap.add_argument("--chapters", required=True, help="first-last, e.g. 61-70")
    ap.add_argument("--book")
    ap.add_argument("--mode", choices=("auto", "full", "scenes"), default="auto",
                    help="auto: scenes when every chapter has locked Hindi, full when none has")
    ap.add_argument("--copy", action="store_true", help="put prompt.txt on the clipboard")
    ap.add_argument("--out", help="write into this folder instead of <book>/gemini/<batch>/")
    a = ap.parse_args(argv)

    try:
        first, last = P.parse_chapters(a.chapters)
        book, _ = context.resolve(a.book)
        r = P.generate(book, first, last, a.mode, Path(a.out) if a.out else None)
    except P.PromptError as e:
        print(f"novel prompt: {e}", file=sys.stderr)
        return 1

    if a.out:
        print(f"WARNING: --out {a.out} is for looking at a prompt, not for real work. `novel import` looks for the "
              f"batch in {book}'s own gemini/ folder and will not find this one. For real work run the same "
              f"command again without --out.")
        print("")
    for note in r["notes"]:
        print(f"note: {note}")
    print(f"Batch {r['batch']} ({r['mode']} mode) for book '{book}', chapters {first}-{last}")
    print(f"Folder: {r['folder']}")
    print("")
    print("Upload these files to Gemini:")
    for f in r["upload"]:
        print(f"  {f}")
    print(f"Prompt: {r['files']['prompt']}  ({len(r['prompt'])} characters)")
    if r["mode"] == "scenes":
        print("")
        print("Windows per chapter (one picture each):")
        for n, c in r["windows"].items():
            print(f"  chapter {n}: {c}")
        print(f"  total pictures: {sum(r['windows'].values())}")
    if r["review"]:
        print("")
        print("The prompt also asks Gemini to clean up old character descriptions (REVIEW TASK).")

    if a.copy:
        print("")
        if set_clipboard(r["prompt"]):
            print("The prompt is on the clipboard (whatever was there before is replaced).")
        else:
            print(f"The clipboard is not available here. Open {r['files']['prompt']} and copy it by hand.")

    print("")
    print("What happens next:")
    print("  1. Open a new Gemini chat, upload the files above, and paste the prompt.")
    print("  2. Gemini replies with the BIBLE block. Copy the reply and run: novel import")
    if r["mode"] == "scenes":
        print("  3. Type  next  in Gemini for each chapter: it replies with that chapter's IMAGES block.")
    else:
        print("  3. Type  next  in Gemini for each chapter: it replies with the HINDI and IMAGES blocks.")
    print("     Copy each reply and run  novel import  after it.")
    print("  4. When a reply ends with a line  >>> MORE ...  type  next  again to get the rest.")
    return 0
