"""Language metadata: writing direction, script detection, fonts, digits.

Everything language-specific in book-translator lives here, so supporting a
new language means adding one row to LANGS (and, for a new script, one entry
to SCRIPTS). Unknown codes still work: they are treated as left-to-right with
no script check.
"""
from __future__ import annotations

import re
import unicodedata

# script name -> character class body (used to build regexes)
SCRIPTS = {
    "latin": "A-Za-zÀ-ɏḀ-ỿ",
    "arabic": "؀-ۿݐ-ݿࢠ-ࣿﭐ-﷿ﹰ-﻿",
    "hebrew": "֐-׿יִ-ﭏ",
    "syriac": "܀-ݏ",
    "thaana": "ހ-޿",
    "cyrillic": "Ѐ-ӿԀ-ԯ",
    "greek": "Ͱ-Ͽἀ-῿",
    "armenian": "԰-֏",
    "georgian": "Ⴀ-ჿ",
    "devanagari": "ऀ-ॿ",
    "bengali": "ঀ-৿",
    "gurmukhi": "਀-੿",
    "gujarati": "઀-૿",
    "oriya": "଀-୿",
    "tamil": "஀-௿",
    "telugu": "ఀ-౿",
    "kannada": "ಀ-೿",
    "malayalam": "ഀ-ൿ",
    "sinhala": "඀-෿",
    "thai": "฀-๿",
    "lao": "຀-໿",
    "tibetan": "ༀ-࿿",
    "myanmar": "က-႟",
    "ethiopic": "ሀ-᎟",
    "khmer": "ក-៿",
    "han": "㐀-䶿一-鿿豈-﫿",
    "kana": "぀-ヿㇰ-ㇿ",
    "hangul": "ᄀ-ᇿ㄰-㆏가-힯",
}

# scripts whose text is not separated by spaces (word counts are meaningless)
UNSPACED = {"han", "kana", "thai", "lao", "khmer", "myanmar", "tibetan"}
RTL_SCRIPTS = {"arabic", "hebrew", "syriac", "thaana"}

# code -> (English name, scripts the language is written in, CSS font stack)
_SANS = '"Segoe UI", "Noto Sans", Arial, sans-serif'
LANGS = {
    "en": ("English", ["latin"], _SANS),
    "fr": ("French", ["latin"], _SANS),
    "es": ("Spanish", ["latin"], _SANS),
    "pt": ("Portuguese", ["latin"], _SANS),
    "it": ("Italian", ["latin"], _SANS),
    "de": ("German", ["latin"], _SANS),
    "nl": ("Dutch", ["latin"], _SANS),
    "sv": ("Swedish", ["latin"], _SANS),
    "no": ("Norwegian", ["latin"], _SANS),
    "da": ("Danish", ["latin"], _SANS),
    "fi": ("Finnish", ["latin"], _SANS),
    "pl": ("Polish", ["latin"], _SANS),
    "cs": ("Czech", ["latin"], _SANS),
    "sk": ("Slovak", ["latin"], _SANS),
    "ro": ("Romanian", ["latin"], _SANS),
    "hu": ("Hungarian", ["latin"], _SANS),
    "hr": ("Croatian", ["latin"], _SANS),
    "tr": ("Turkish", ["latin"], _SANS),
    "id": ("Indonesian", ["latin"], _SANS),
    "ms": ("Malay", ["latin"], _SANS),
    "vi": ("Vietnamese", ["latin"], _SANS),
    "sw": ("Swahili", ["latin"], _SANS),
    "tl": ("Tagalog", ["latin"], _SANS),
    "az": ("Azerbaijani", ["latin"], _SANS),
    "uz": ("Uzbek", ["latin"], _SANS),
    "ru": ("Russian", ["cyrillic"], _SANS),
    "uk": ("Ukrainian", ["cyrillic"], _SANS),
    "bg": ("Bulgarian", ["cyrillic"], _SANS),
    "sr": ("Serbian", ["cyrillic", "latin"], _SANS),
    "kk": ("Kazakh", ["cyrillic"], _SANS),
    "el": ("Greek", ["greek"], _SANS),
    "hy": ("Armenian", ["armenian"], '"Noto Sans Armenian", ' + _SANS),
    "ka": ("Georgian", ["georgian"], '"Noto Sans Georgian", "Sylfaen", ' + _SANS),
    "ar": ("Arabic", ["arabic"], '"Noto Naskh Arabic", "Segoe UI", "Arial", "Tahoma", sans-serif'),
    "fa": ("Persian", ["arabic"], '"Vazirmatn", "Noto Naskh Arabic", "Segoe UI", Tahoma, sans-serif'),
    "ur": ("Urdu", ["arabic"], '"Noto Nastaliq Urdu", "Jameel Noori Nastaleeq", "Segoe UI", Tahoma, sans-serif'),
    "ps": ("Pashto", ["arabic"], '"Noto Naskh Arabic", "Segoe UI", Tahoma, sans-serif'),
    "ku": ("Kurdish (Sorani)", ["arabic"], '"Noto Naskh Arabic", "Segoe UI", Tahoma, sans-serif'),
    "ug": ("Uyghur", ["arabic"], '"Noto Naskh Arabic", "Segoe UI", Tahoma, sans-serif'),
    "he": ("Hebrew", ["hebrew"], '"Noto Sans Hebrew", "Segoe UI", Arial, sans-serif'),
    "yi": ("Yiddish", ["hebrew"], '"Noto Sans Hebrew", "Segoe UI", Arial, sans-serif'),
    "dv": ("Dhivehi", ["thaana"], '"MV Boli", "Noto Sans Thaana", sans-serif'),
    "hi": ("Hindi", ["devanagari"], '"Noto Sans Devanagari", "Nirmala UI", "Mangal", sans-serif'),
    "mr": ("Marathi", ["devanagari"], '"Noto Sans Devanagari", "Nirmala UI", sans-serif'),
    "ne": ("Nepali", ["devanagari"], '"Noto Sans Devanagari", "Nirmala UI", sans-serif'),
    "bn": ("Bengali", ["bengali"], '"Noto Sans Bengali", "Nirmala UI", "Vrinda", sans-serif'),
    "pa": ("Punjabi", ["gurmukhi"], '"Noto Sans Gurmukhi", "Nirmala UI", sans-serif'),
    "gu": ("Gujarati", ["gujarati"], '"Noto Sans Gujarati", "Nirmala UI", sans-serif'),
    "or": ("Odia", ["oriya"], '"Noto Sans Oriya", "Nirmala UI", sans-serif'),
    "ta": ("Tamil", ["tamil"], '"Noto Sans Tamil", "Nirmala UI", "Latha", sans-serif'),
    "te": ("Telugu", ["telugu"], '"Noto Sans Telugu", "Nirmala UI", "Gautami", sans-serif'),
    "kn": ("Kannada", ["kannada"], '"Noto Sans Kannada", "Nirmala UI", "Tunga", sans-serif'),
    "ml": ("Malayalam", ["malayalam"], '"Noto Sans Malayalam", "Nirmala UI", "Kartika", sans-serif'),
    "si": ("Sinhala", ["sinhala"], '"Noto Sans Sinhala", "Nirmala UI", "Iskoola Pota", sans-serif'),
    "th": ("Thai", ["thai"], '"Noto Sans Thai", "Leelawadee UI", "Tahoma", sans-serif'),
    "lo": ("Lao", ["lao"], '"Noto Sans Lao", "Leelawadee UI", sans-serif'),
    "km": ("Khmer", ["khmer"], '"Noto Sans Khmer", "Leelawadee UI", "Khmer UI", sans-serif'),
    "my": ("Burmese", ["myanmar"], '"Noto Sans Myanmar", "Myanmar Text", sans-serif'),
    "am": ("Amharic", ["ethiopic"], '"Noto Sans Ethiopic", "Ebrima", "Nyala", sans-serif'),
    "bo": ("Tibetan", ["tibetan"], '"Noto Serif Tibetan", "Microsoft Himalaya", serif'),
    "zh": ("Chinese (Simplified)", ["han"], '"Noto Sans SC", "Microsoft YaHei", "PingFang SC", "SimSun", sans-serif'),
    "zh-tw": ("Chinese (Traditional)", ["han"], '"Noto Sans TC", "Microsoft JhengHei", "PingFang TC", sans-serif'),
    "ja": ("Japanese", ["kana", "han"], '"Noto Sans JP", "Yu Gothic", "Meiryo", "Hiragino Sans", sans-serif'),
    "ko": ("Korean", ["hangul"], '"Noto Sans KR", "Malgun Gothic", "Apple SD Gothic Neo", sans-serif'),
}

ALIASES = {"zh-cn": "zh", "zh-hans": "zh", "zh-hant": "zh-tw", "iw": "he",
           "pt-br": "pt", "pt-pt": "pt", "en-us": "en", "en-gb": "en",
           "ckb": "ku", "nb": "no", "fil": "tl"}

MONO_STACK = '"Cascadia Mono", "JetBrains Mono", Consolas, "DejaVu Sans Mono", "Courier New", monospace'

# Native digit sets, used to compare numbers across languages.
_DIGIT_SETS = ["٠١٢٣٤٥٦٧٨٩", "۰۱۲۳۴۵۶۷۸۹", "०१२३४५६७८९", "০১২৩৪৫৬৭৮৯",
               "੦੧੨੩੪੫੬੭੮੯", "૦૧૨૩૪૫૬૭૮૯", "௦௧௨௩௪௫௬௭௮௯", "౦౧౨౩౪౫౬౭౮౯",
               "೦೧೨೩೪೫೬೭೮೯", "൦൧൨൩൪൫൬൭൮൯", "๐๑๒๓๔๕๖๗๘๙", "໐໑໒໓໔໕໖໗໘໙",
               "០១២៣៤៥៦៧៨៩", "၀၁၂၃၄၅၆၇၈၉", "０１２３４５６７８９"]
_TO_ASCII = {}
for _set in _DIGIT_SETS:
    for _i, _ch in enumerate(_set):
        _TO_ASCII[ord(_ch)] = str(_i)
_TO_ASCII[ord("٫")] = "."   # Arabic decimal separator
_TO_ASCII[ord("٬")] = ","   # Arabic thousands separator


def norm(code: str) -> str:
    code = (code or "").strip().lower().replace("_", "-")
    code = ALIASES.get(code, code)
    if code in LANGS:
        return code
    base = code.split("-")[0]
    return base if base in LANGS else code


def info(code: str) -> dict:
    """Return name, scripts, direction and font stack for a language code."""
    code = norm(code)
    name, scripts, fonts = LANGS.get(code, (code, [], _SANS))
    rtl = bool(scripts) and scripts[0] in RTL_SCRIPTS
    return {"code": code, "name": name, "scripts": scripts,
            "dir": "rtl" if rtl else "ltr", "fonts": fonts,
            "unspaced": any(s in UNSPACED for s in scripts),
            "known": code in LANGS}


def script_re(scripts) -> re.Pattern | None:
    body = "".join(SCRIPTS[s] for s in scripts if s in SCRIPTS)
    return re.compile(f"[{body}]") if body else None


RTL_RE = script_re(RTL_SCRIPTS)
LATIN_RE = script_re(["latin"])


def ascii_digits(text: str) -> str:
    return text.translate(_TO_ASCII)


def script_counts(text: str) -> dict:
    """Count letters per script (only scripts that occur)."""
    counts = {}
    for name, body in SCRIPTS.items():
        n = len(re.findall(f"[{body}]", text))
        if n:
            counts[name] = n
    return counts


def estimate_tokens(text: str) -> int:
    """Rough LLM token estimate that works across scripts.

    Spaced scripts average ~4 characters per token; CJK, Thai and other
    unspaced scripts are closer to 1-1.5 characters per token.
    """
    if not text:
        return 0
    unspaced = sum(len(re.findall(f"[{SCRIPTS[s]}]", text)) for s in UNSPACED)
    rest = len(text) - unspaced
    return int(rest / 4 + unspaced / 1.2) + 1


def word_count(text: str) -> int:
    """Words for spaced scripts; characters/2 for unspaced ones."""
    unspaced = sum(len(re.findall(f"[{SCRIPTS[s]}]", text)) for s in UNSPACED)
    words = len(re.findall(r"\w+", re.sub(f"[{''.join(SCRIPTS[s] for s in UNSPACED)}]", " ", text)))
    return words + unspaced // 2


# A few very frequent words per Latin-script language, enough to tell them apart.
_STOP = {
    "en": "the and of to is in that it for with as are this be on not you was have from by or will can your which an",
    "fr": "le la les et des est une que dans pour pas qui sur du avec",
    "es": "el la los las de que y en un una es por para con del se",
    "pt": "o a os as de que e em um uma para com não do da se",
    "it": "il la di che e un una per non sono del della con si gli",
    "de": "der die das und ist nicht ein eine zu mit den von sie es auf",
    "nl": "de het een en van is dat op te niet zijn voor met die",
    "sv": "och att det som en är på för med inte av till den",
    "pl": "i w nie się na to że jest do z jak co ale",
    "tr": "ve bir bu da de için ile çok ne gibi olarak daha",
    "id": "dan yang di ini itu dengan untuk tidak dari dalam akan",
}


def detect(text: str) -> str:
    """Best-effort source-language guess from a text sample."""
    counts = script_counts(text)
    if not counts:
        return "en"
    main = max(counts, key=counts.get)
    if main == "kana" or ("han" in counts and "kana" in counts):
        return "ja"
    by_script = {"arabic": "ar", "hebrew": "he", "cyrillic": "ru", "greek": "el",
                 "han": "zh", "hangul": "ko", "devanagari": "hi", "thai": "th",
                 "bengali": "bn", "tamil": "ta", "armenian": "hy",
                 "georgian": "ka", "ethiopic": "am"}
    if main != "latin":
        return by_script.get(main, "en")
    words = re.findall(r"[^\W\d_]+", text.lower())
    best, score = "en", -1
    for code, stop in _STOP.items():
        s = set(stop.split())
        n = sum(1 for w in words if w in s)
        if n > score:
            best, score = code, n
    return best


def strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s)
                   if unicodedata.category(c) != "Mn")
