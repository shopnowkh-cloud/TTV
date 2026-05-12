import os
import re
import json
import unicodedata
import asyncio
import logging
from io import BytesIO
from collections import OrderedDict
import edge_tts
import imageio_ffmpeg

_FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()

from langdetect import detect as langdetect_detect, detect_langs, DetectorFactory
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, constants
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters
from telegram.request import HTTPXRequest

def strip_unspeakable(text: str) -> str:
    """Remove emojis and other symbols that TTS engines cannot pronounce.
    
    Keeps: letters (L*), combining marks (M* — essential for Khmer, Thai,
    Devanagari, Arabic etc.), numbers (N*), punctuation (P*), separators (Z*).
    Strips: symbols (S*) which includes emojis, currency, math symbols, etc.
    Also strips control/format chars (C*) except whitespace.
    """
    result = []
    for ch in text:
        cat = unicodedata.category(ch)
        # Keep letters, combining marks, numbers, punctuation, separators
        if cat.startswith(('L', 'M', 'N', 'P', 'Z')):
            result.append(ch)
        elif ch in ('\n', '\r', '\t', ' '):
            result.append(ch)
        # Drop: S* (symbols/emoji), C* (control/format) except spaces above
    return ''.join(result)

def has_speakable_content(text: str) -> bool:
    """Return True only if the text contains at least one letter or digit."""
    return bool(re.search(r'\w', text, re.UNICODE))

# Cache Telegram file_id for repeated text — avoids re-upload (instant resend)
_FILE_ID_CACHE: OrderedDict[str, str] = OrderedDict()
_CACHE_MAX = 200

def _cache_get(key: str):
    if key in _FILE_ID_CACHE:
        _FILE_ID_CACHE.move_to_end(key)
        return _FILE_ID_CACHE[key]
    return None

def _cache_set(key: str, file_id: str):
    if key in _FILE_ID_CACHE:
        _FILE_ID_CACHE.move_to_end(key)
    else:
        if len(_FILE_ID_CACHE) >= _CACHE_MAX:
            _FILE_ID_CACHE.popitem(last=False)
        _FILE_ID_CACHE[key] = file_id

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

DetectorFactory.seed = 0

# ─── Persistent user preferences (survives bot restarts) ──────────────────────
_PREFS_FILE = os.path.join(os.path.dirname(__file__), "user_prefs.json")
_user_prefs: dict = {}

def _load_prefs():
    global _user_prefs
    try:
        if os.path.exists(_PREFS_FILE):
            with open(_PREFS_FILE, "r", encoding="utf-8") as f:
                _user_prefs = json.load(f)
    except Exception:
        _user_prefs = {}

def _save_prefs():
    try:
        with open(_PREFS_FILE, "w", encoding="utf-8") as f:
            json.dump(_user_prefs, f)
    except Exception as e:
        logging.warning(f"Could not save user prefs: {e}")

def get_gender(user_id: int) -> str:
    return _user_prefs.get(str(user_id), "female")

def set_gender(user_id: int, gender: str):
    _user_prefs[str(user_id)] = gender
    _save_prefs()

_load_prefs()

# ─── New-user tracking & admin notification ────────────────────────────────────
ADMIN_ID = 5002402843
_KNOWN_USERS_FILE = os.path.join(os.path.dirname(__file__), "known_users.json")
_known_users: set = set()

def _load_known_users():
    global _known_users
    try:
        if os.path.exists(_KNOWN_USERS_FILE):
            with open(_KNOWN_USERS_FILE, "r", encoding="utf-8") as f:
                _known_users = set(json.load(f))
    except Exception:
        _known_users = set()

def _save_known_users():
    try:
        with open(_KNOWN_USERS_FILE, "w", encoding="utf-8") as f:
            json.dump(list(_known_users), f)
    except Exception as e:
        logging.warning(f"Could not save known users: {e}")

def is_new_user(user_id: int) -> bool:
    return str(user_id) not in _known_users

def mark_user_known(user_id: int):
    _known_users.add(str(user_id))
    _save_known_users()

async def notify_admin_new_user(bot, user):
    try:
        username = f"@{user.username}" if user.username else "គ្មាន username"
        full_name = user.full_name or "គ្មានឈ្មោះ"
        msg = (
            f"🆕 <b>អ្នកប្រើប្រាស់ថ្មី!</b>\n\n"
            f"👤 <b>ឈ្មោះ:</b> {full_name}\n"
            f"🔖 <b>Username:</b> {username}\n"
            f"🪪 <b>ID:</b> <code>{user.id}</code>"
        )
        await bot.send_message(chat_id=ADMIN_ID, text=msg, parse_mode="HTML")
    except Exception as e:
        logging.warning(f"Could not notify admin: {e}")

_load_known_users()

# All available edge-tts voices — male and female per language
MALE_VOICES = {
    "af":    "af-ZA-WillemNeural",
    "am":    "am-ET-AmehaNeural",
    "ar":    "ar-SA-HamedNeural",
    "az":    "az-AZ-BabekNeural",
    "bg":    "bg-BG-BorislavNeural",
    "bn":    "bn-BD-PradeepNeural",
    "bs":    "bs-BA-GoranNeural",
    "ca":    "ca-ES-EnricNeural",
    "cs":    "cs-CZ-AntoninNeural",
    "cy":    "cy-GB-AledNeural",
    "da":    "da-DK-JeppeNeural",
    "de":    "de-DE-FlorianMultilingualNeural",
    "el":    "el-GR-NestorasNeural",
    "en":    "en-US-AndrewMultilingualNeural",
    "es":    "es-ES-AlvaroNeural",
    "et":    "et-EE-KertNeural",
    "fa":    "fa-IR-FaridNeural",
    "fi":    "fi-FI-HarriNeural",
    "fil":   "fil-PH-AngeloNeural",
    "fr":    "fr-FR-RemyMultilingualNeural",
    "ga":    "ga-IE-ColmNeural",
    "gl":    "gl-ES-RoiNeural",
    "gu":    "gu-IN-NiranjanNeural",
    "he":    "he-IL-AvriNeural",
    "hi":    "hi-IN-MadhurNeural",
    "hr":    "hr-HR-SreckoNeural",
    "hu":    "hu-HU-TamasNeural",
    "id":    "id-ID-ArdiNeural",
    "is":    "is-IS-GunnarNeural",
    "it":    "it-IT-GiuseppeMultilingualNeural",
    "ja":    "ja-JP-KeitaNeural",
    "jv":    "jv-ID-DimasNeural",
    "ka":    "ka-GE-GiorgiNeural",
    "kk":    "kk-KZ-DauletNeural",
    "km":    "km-KH-PisethNeural",
    "kn":    "kn-IN-GaganNeural",
    "ko":    "ko-KR-HyunsuMultilingualNeural",
    "lo":    "lo-LA-ChanthavongNeural",
    "lt":    "lt-LT-LeonasNeural",
    "lv":    "lv-LV-NilsNeural",
    "mk":    "mk-MK-AleksandarNeural",
    "ml":    "ml-IN-MidhunNeural",
    "mn":    "mn-MN-BataaNeural",
    "mr":    "mr-IN-ManoharNeural",
    "ms":    "ms-MY-OsmanNeural",
    "mt":    "mt-MT-JosephNeural",
    "my":    "my-MM-ThihaNeural",
    "nb":    "nb-NO-FinnNeural",
    "ne":    "ne-NP-SagarNeural",
    "nl":    "nl-NL-MaartenNeural",
    "pl":    "pl-PL-MarekNeural",
    "ps":    "ps-AF-GulNawazNeural",
    "pt":    "pt-BR-AntonioNeural",
    "ro":    "ro-RO-EmilNeural",
    "ru":    "ru-RU-DmitryNeural",
    "si":    "si-LK-SameeraNeural",
    "sk":    "sk-SK-LukasNeural",
    "sl":    "sl-SI-RokNeural",
    "so":    "so-SO-MuuseNeural",
    "sq":    "sq-AL-IlirNeural",
    "sr":    "sr-RS-NicholasNeural",
    "su":    "su-ID-JajangNeural",
    "sv":    "sv-SE-MattiasNeural",
    "sw":    "sw-KE-RafikiNeural",
    "ta":    "ta-IN-ValluvarNeural",
    "te":    "te-IN-MohanNeural",
    "th":    "th-TH-NiwatNeural",
    "tr":    "tr-TR-AhmetNeural",
    "uk":    "uk-UA-OstapNeural",
    "ur":    "ur-IN-SalmanNeural",
    "uz":    "uz-UZ-SardorNeural",
    "vi":    "vi-VN-NamMinhNeural",
    "zh-CN": "zh-CN-YunyangNeural",
    "zh-TW": "zh-TW-YunJheNeural",
    "zu":    "zu-ZA-ThembaNeural",
}

FEMALE_VOICES = {
    "af":    "af-ZA-AdriNeural",
    "am":    "am-ET-MekdesNeural",
    "ar":    "ar-SA-ZariyahNeural",
    "az":    "az-AZ-BanuNeural",
    "bg":    "bg-BG-KalinaNeural",
    "bn":    "bn-BD-NabanitaNeural",
    "bs":    "bs-BA-VesnaNeural",
    "ca":    "ca-ES-JoanaNeural",
    "cs":    "cs-CZ-VlastaNeural",
    "cy":    "cy-GB-NiaNeural",
    "da":    "da-DK-ChristelNeural",
    "de":    "de-DE-SeraphinaMultilingualNeural",
    "el":    "el-GR-AthinaNeural",
    "en":    "en-US-AvaMultilingualNeural",
    "es":    "es-ES-XimenaNeural",
    "et":    "et-EE-AnuNeural",
    "fa":    "fa-IR-DilaraNeural",
    "fi":    "fi-FI-NooraNeural",
    "fil":   "fil-PH-BlessicaNeural",
    "fr":    "fr-FR-VivienneMultilingualNeural",
    "ga":    "ga-IE-OrlaNeural",
    "gl":    "gl-ES-SabelaNeural",
    "gu":    "gu-IN-DhwaniNeural",
    "he":    "he-IL-HilaNeural",
    "hi":    "hi-IN-SwaraNeural",
    "hr":    "hr-HR-GabrijelaNeural",
    "hu":    "hu-HU-NoemiNeural",
    "id":    "id-ID-GadisNeural",
    "is":    "is-IS-GudrunNeural",
    "it":    "it-IT-IsabellaNeural",
    "ja":    "ja-JP-NanamiNeural",
    "jv":    "jv-ID-SitiNeural",
    "ka":    "ka-GE-EkaNeural",
    "kk":    "kk-KZ-AigulNeural",
    "km":    "km-KH-SreymomNeural",
    "kn":    "kn-IN-SapnaNeural",
    "ko":    "ko-KR-SunHiNeural",
    "lo":    "lo-LA-KeomanyNeural",
    "lt":    "lt-LT-OnaNeural",
    "lv":    "lv-LV-EveritaNeural",
    "mk":    "mk-MK-MarijaNeural",
    "ml":    "ml-IN-SobhanaNeural",
    "mn":    "mn-MN-YesuiNeural",
    "mr":    "mr-IN-AarohiNeural",
    "ms":    "ms-MY-YasminNeural",
    "mt":    "mt-MT-GraceNeural",
    "my":    "my-MM-NilarNeural",
    "nb":    "nb-NO-PernilleNeural",
    "ne":    "ne-NP-HemkalaNeural",
    "nl":    "nl-NL-ColetteNeural",
    "pl":    "pl-PL-ZofiaNeural",
    "ps":    "ps-AF-LatifaNeural",
    "pt":    "pt-BR-ThalitaMultilingualNeural",
    "ro":    "ro-RO-AlinaNeural",
    "ru":    "ru-RU-SvetlanaNeural",
    "si":    "si-LK-ThiliniNeural",
    "sk":    "sk-SK-ViktoriaNeural",
    "sl":    "sl-SI-PetraNeural",
    "so":    "so-SO-UbaxNeural",
    "sq":    "sq-AL-AnilaNeural",
    "sr":    "sr-RS-SophieNeural",
    "su":    "su-ID-TutiNeural",
    "sv":    "sv-SE-SofieNeural",
    "sw":    "sw-KE-ZuriNeural",
    "ta":    "ta-IN-PallaviNeural",
    "te":    "te-IN-ShrutiNeural",
    "th":    "th-TH-PremwadeeNeural",
    "tr":    "tr-TR-EmelNeural",
    "uk":    "uk-UA-PolinaNeural",
    "ur":    "ur-IN-GulNeural",
    "uz":    "uz-UZ-MadinaNeural",
    "vi":    "vi-VN-HoaiMyNeural",
    "zh-CN": "zh-CN-XiaoxiaoNeural",
    "zh-TW": "zh-TW-HsiaoChenNeural",
    "zu":    "zu-ZA-ThandoNeural",
}

LANG_NAMES = {
    "af": "Afrikaans", "am": "Amharic (አማርኛ)", "ar": "Arabic (العربية)",
    "az": "Azerbaijani", "bg": "Bulgarian", "bn": "Bengali (বাংলা)",
    "bs": "Bosnian", "ca": "Catalan", "cs": "Czech", "cy": "Welsh",
    "da": "Danish", "de": "German", "el": "Greek (Ελληνικά)",
    "en": "English", "es": "Spanish", "et": "Estonian",
    "fa": "Persian (فارسی)", "fi": "Finnish", "fil": "Filipino",
    "fr": "French", "ga": "Irish", "gl": "Galician",
    "gu": "Gujarati (ગુજરાતી)", "he": "Hebrew (עברית)", "hi": "Hindi (हिंदी)",
    "hr": "Croatian", "hu": "Hungarian", "id": "Indonesian",
    "is": "Icelandic", "it": "Italian", "ja": "Japanese (日本語)",
    "jv": "Javanese", "ka": "Georgian (ქართული)", "kk": "Kazakh",
    "km": "ខ្មែរ (Khmer)", "kn": "Kannada (ಕನ್ನಡ)", "ko": "Korean (한국어)",
    "lo": "Lao (ລາວ)", "lt": "Lithuanian", "lv": "Latvian",
    "mk": "Macedonian", "ml": "Malayalam (മലയാളം)", "mn": "Mongolian",
    "mr": "Marathi (मराठी)", "ms": "Malay", "mt": "Maltese",
    "my": "Myanmar (မြန်မာ)", "nb": "Norwegian", "ne": "Nepali (नेपाली)",
    "nl": "Dutch", "pl": "Polish", "ps": "Pashto (پښتو)",
    "pt": "Portuguese", "ro": "Romanian", "ru": "Russian (Русский)",
    "si": "Sinhala (සිංහල)", "sk": "Slovak", "sl": "Slovenian",
    "so": "Somali", "sq": "Albanian", "sr": "Serbian",
    "su": "Sundanese", "sv": "Swedish", "sw": "Swahili",
    "ta": "Tamil (தமிழ்)", "te": "Telugu (తెలుగు)", "th": "Thai (ภาษาไทย)",
    "tr": "Turkish", "uk": "Ukrainian", "ur": "Urdu (اردو)",
    "uz": "Uzbek", "vi": "Vietnamese", "zh-CN": "Chinese (中文简体)",
    "zh-TW": "Chinese (中文繁體)", "zu": "Zulu",
}

# Normalize langdetect output → our internal code
NORMALIZE = {
    "zh-cn": "zh-CN", "zh-tw": "zh-TW", "zh": "zh-CN",
    "iw": "he", "no": "nb", "tl": "fil", "jw": "jv", "in": "id",
}

# Languages detected by script but not supported by edge-tts → map to closest voice
LANG_FALLBACK = {
    "pa": "hi",   # Punjabi (Gurmukhi) → Hindi (closest supported voice)
    "or": "bn",   # Oriya → Bengali (closest supported voice)
    "hy": "en",   # Armenian → English (no close option in edge-tts)
}

# Script-based detection using Unicode ranges (faster & more reliable than langdetect)
SCRIPT_MAP = [
    (r'[\u1780-\u17FF]', 'km'),        # Khmer
    (r'[\u0E00-\u0E7F]', 'th'),        # Thai
    (r'[\u0E80-\u0EFF]', 'lo'),        # Lao
    (r'[\u1000-\u109F]', 'my'),        # Myanmar
    (r'[\u1200-\u137F]', 'am'),        # Ethiopic → Amharic
    (r'[\u10A0-\u10FF]', 'ka'),        # Georgian
    (r'[\u0530-\u058F]', 'hy'),        # Armenian (no edge-tts, fallback en)
    (r'[\u0590-\u05FF]', 'he'),        # Hebrew
    (r'[\u0900-\u097F]', 'hi'),        # Devanagari → Hindi
    (r'[\u0980-\u09FF]', 'bn'),        # Bengali
    (r'[\u0A00-\u0A7F]', 'pa'),        # Gurmukhi → Punjabi
    (r'[\u0A80-\u0AFF]', 'gu'),        # Gujarati
    (r'[\u0B00-\u0B7F]', 'or'),        # Oriya
    (r'[\u0B80-\u0BFF]', 'ta'),        # Tamil
    (r'[\u0C00-\u0C7F]', 'te'),        # Telugu
    (r'[\u0C80-\u0CFF]', 'kn'),        # Kannada
    (r'[\u0D00-\u0D7F]', 'ml'),        # Malayalam
    (r'[\u0D80-\u0DFF]', 'si'),        # Sinhala
    (r'[\u0600-\u06FF]', 'ar'),        # Arabic script (ar/fa/ur/ps)
    (r'[\u0750-\u077F\uFB50-\uFDFF\uFE70-\uFEFF]', 'ar'),  # Arabic extended
    (r'[\u0400-\u04FF]', 'ru'),        # Cyrillic → Russian (fallback)
    (r'[\u0370-\u03FF]', 'el'),        # Greek
    (r'[\u1800-\u18AF]', 'mn'),        # Mongolian script
    (r'[\uAC00-\uD7FF]', 'ko'),        # Korean Hangul
    (r'[\u3040-\u30FF]', 'ja'),        # Japanese Hiragana/Katakana
    (r'[\u4E00-\u9FFF\u3400-\u4DBF]', 'zh-CN'),  # CJK
]

# ─── Mixed-language segmentation ──────────────────────────────────────────────
_SEGMENT_RE = re.compile(
    r'(?P<km>[\u1780-\u17FF]+)'
    r'|(?P<th>[\u0E00-\u0E7F]+)'
    r'|(?P<lo>[\u0E80-\u0EFF]+)'
    r'|(?P<my>[\u1000-\u109F]+)'
    r'|(?P<am>[\u1200-\u137F]+)'
    r'|(?P<ka>[\u10A0-\u10FF]+)'
    r'|(?P<he>[\u0590-\u05FF]+)'
    r'|(?P<hi>[\u0900-\u097F]+)'
    r'|(?P<bn>[\u0980-\u09FF]+)'
    r'|(?P<pa>[\u0A00-\u0A7F]+)'
    r'|(?P<gu>[\u0A80-\u0AFF]+)'
    r'|(?P<ta>[\u0B80-\u0BFF]+)'
    r'|(?P<te>[\u0C00-\u0C7F]+)'
    r'|(?P<kn>[\u0C80-\u0CFF]+)'
    r'|(?P<ml>[\u0D00-\u0D7F]+)'
    r'|(?P<si>[\u0D80-\u0DFF]+)'
    r'|(?P<ar>[\u0600-\u06FF\u0750-\u077F\uFB50-\uFDFF\uFE70-\uFEFF]+)'
    r'|(?P<ru>[\u0400-\u04FF]+)'
    r'|(?P<el>[\u0370-\u03FF]+)'
    r'|(?P<mn_s>[\u1800-\u18AF]+)'
    r'|(?P<ko>[\uAC00-\uD7FF]+)'
    r'|(?P<ja>[\u3040-\u30FF]+)'
    r'|(?P<zh>[\u4E00-\u9FFF\u3400-\u4DBF]+)'
    r'|(?P<other>[^\u1780-\u17FF\u0E00-\u0EFF\u1000-\u109F\u1200-\u137F'
    r'\u10A0-\u10FF\u0590-\u05FF\u0900-\u09FF\u0A00-\u0AFF\u0B80-\u0BFF'
    r'\u0C00-\u0CFF\u0D00-\u0DFF\u0600-\u06FF\u0750-\u077F\uFB50-\uFDFF'
    r'\uFE70-\uFEFF\u0400-\u04FF\u0370-\u03FF\u1800-\u18AF\uAC00-\uD7FF'
    r'\u3040-\u30FF\u4E00-\u9FFF\u3400-\u4DBF]+)'
)
_SCRIPT_LANG = {
    'km': 'km', 'th': 'th', 'lo': 'lo', 'my': 'my', 'am': 'am',
    'ka': 'ka', 'he': 'he', 'hi': 'hi', 'bn': 'bn', 'pa': 'hi',
    'gu': 'gu', 'ta': 'ta', 'te': 'te', 'kn': 'kn', 'ml': 'ml',
    'si': 'si', 'ar': 'ar', 'ru': 'ru', 'el': 'el', 'mn_s': 'mn',
    'ko': 'ko', 'ja': 'ja', 'zh': 'zh-CN',
}

def segment_text(text: str) -> list:
    """Split text into [(chunk, lang)] by script. Merges adjacent same-lang segments."""
    raw = []
    for m in _SEGMENT_RE.finditer(text):
        g = m.lastgroup
        chunk = m.group()
        if g == 'other':
            raw.append((chunk, None))
        else:
            lang = _SCRIPT_LANG.get(g, 'en')
            # Refine Arabic-script
            if g == 'ar':
                try:
                    d = NORMALIZE.get(langdetect_detect(chunk), langdetect_detect(chunk))
                    if d in ('fa', 'ur', 'ps', 'ar'):
                        lang = d
                except Exception:
                    pass
            # Refine Cyrillic
            elif g == 'ru':
                try:
                    d = NORMALIZE.get(langdetect_detect(chunk), langdetect_detect(chunk))
                    if d in ('ru', 'uk', 'bg', 'sr', 'mk', 'kk', 'mn'):
                        lang = d
                except Exception:
                    pass
            raw.append((chunk, lang))

    # Resolve Latin/other segments
    resolved = []
    for chunk, lang in raw:
        if lang is not None:
            resolved.append((chunk, lang))
            continue
        stripped = chunk.strip()
        if not stripped:
            # Pure whitespace — attach to previous segment
            if resolved:
                resolved[-1] = (resolved[-1][0] + chunk, resolved[-1][1])
            continue
        # If chunk has no actual Latin letters (only digits, punctuation, emoji, etc.)
        # absorb it into the previous segment so it is read in the same language context
        has_latin_letters = bool(re.search(r'[a-zA-Z]', chunk))
        if not has_latin_letters:
            if resolved:
                resolved[-1] = (resolved[-1][0] + chunk, resolved[-1][1])
            else:
                resolved.append((chunk, 'en'))
            continue
        detected = 'en'
        if len(stripped) >= 4:
            try:
                langs = detect_langs(stripped)
                if langs and langs[0].prob >= 0.65:
                    detected = NORMALIZE.get(langs[0].lang, langs[0].lang)
            except Exception:
                pass
        resolved.append((chunk, detected))

    # Apply LANG_FALLBACK
    resolved = [(c, LANG_FALLBACK.get(l, l)) for c, l in resolved]

    # Merge adjacent same-language segments
    merged = []
    for chunk, lang in resolved:
        if merged and merged[-1][1] == lang:
            merged[-1] = (merged[-1][0] + chunk, lang)
        else:
            merged.append([chunk, lang])

    return [(c, l) for c, l in merged] if merged else [('', 'en')]

KEYBOARD = ReplyKeyboardMarkup(
    [[KeyboardButton("👨 សំឡេងប្រុស"), KeyboardButton("👩 សំឡេងស្រី")]],
    resize_keyboard=True
)

def detect_language(text: str) -> str:
    # 1. Try script-based detection first (instant & reliable)
    for pattern, lang in SCRIPT_MAP:
        if re.search(pattern, text):
            # Refine Arabic-script languages using langdetect
            if lang == 'ar':
                try:
                    detected = langdetect_detect(text)
                    detected = NORMALIZE.get(detected, detected)
                    if detected in ('fa', 'ur', 'ps', 'ar'):
                        return detected
                except Exception:
                    pass
            # Refine Cyrillic languages using langdetect
            if lang == 'ru':
                try:
                    detected = langdetect_detect(text)
                    detected = NORMALIZE.get(detected, detected)
                    if detected in ('ru', 'uk', 'bg', 'sr', 'mk', 'kk', 'mn'):
                        return detected
                except Exception:
                    pass
            return lang

    # 2. For Latin-script text: use confidence threshold
    # Very short texts are unreliable — default to English
    stripped = text.strip()
    if len(stripped) < 15 or len(stripped.split()) < 3:
        return 'en'

    try:
        langs = detect_langs(text)
        if langs:
            top = langs[0]
            lang_code = NORMALIZE.get(top.lang, top.lang)
            # Accept detection only if confidence >= 0.70, else default English
            if top.prob >= 0.70:
                return lang_code
    except Exception:
        pass

    return 'en'

def voice_rate(lang: str) -> str:
    """Return the TTS speaking rate for a language."""
    return '+0%'

async def _synth_segment_pcm(text: str, voice: str, lang: str = 'en') -> bytes:
    """Synthesize one segment to raw PCM s16le 48000Hz mono via bundled ffmpeg."""
    text = strip_unspeakable(text).strip()
    if not text or not has_speakable_content(text):
        return b''
    try:
        proc = await asyncio.create_subprocess_exec(
            _FFMPEG, "-y", "-f", "mp3", "-i", "pipe:0",
            "-ac", "1", "-ar", "48000", "-f", "s16le", "pipe:1",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        communicate = edge_tts.Communicate(text, voice, rate=voice_rate(lang), pitch="+5Hz")
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                proc.stdin.write(chunk["data"])
        proc.stdin.close()
        stdout, _ = await proc.communicate()
        return stdout
    except Exception as e:
        logging.warning(f"Skipping segment due to error: {e!r} | text={text[:30]!r}")
        return b''

async def _pcm_to_ogg(pcm: bytes) -> BytesIO:
    """Encode concatenated PCM bytes to OGG Opus via bundled ffmpeg."""
    proc = await asyncio.create_subprocess_exec(
        _FFMPEG, "-y", "-f", "s16le", "-ac", "1", "-ar", "48000", "-i", "pipe:0",
        "-c:a", "libopus", "-b:a", "128k", "-f", "ogg", "pipe:1",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await proc.communicate(input=pcm)
    return BytesIO(stdout)

async def synthesize_to_bytes(text: str, voice: str, lang: str = 'en') -> BytesIO:
    """Synthesize text to OGG Opus voice bytes."""
    pcm = await _synth_segment_pcm(text, voice, lang=lang)
    return await _pcm_to_ogg(pcm)

async def synthesize_mixed(segments: list, voice_map: dict) -> BytesIO:
    """Synthesize multiple-language segments in parallel, return one OGG Opus."""
    tasks = [
        _synth_segment_pcm(chunk, voice_map.get(lang) or voice_map.get('en'), lang=lang)
        for chunk, lang in segments
        if strip_unspeakable(chunk).strip() and has_speakable_content(strip_unspeakable(chunk))
    ]
    if not tasks:
        return BytesIO(b'')
    pcm_parts = await asyncio.gather(*tasks)
    return await _pcm_to_ogg(b''.join(pcm_parts))

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logging.error(f"Exception while handling update: {context.error}", exc_info=context.error)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if is_new_user(user.id):
        mark_user_known(user.id)
        asyncio.create_task(notify_admin_new_user(context.bot, user))
    last_name = user.last_name or user.first_name or "បងប្អូន"
    await update.message.reply_text(
        f'<tg-emoji emoji-id="5472055112702629499">👋</tg-emoji> <b>សួស្តី</b> {last_name}\n\n'
        '<b>ខ្ញុំជា Text to voice bot</b>\n\n'
        '<tg-emoji emoji-id="5471978009449731768">👉</tg-emoji><i>គ្រាន់តែ សរសេរអក្សរណាមួយ ហើយ ខ្ញុំនឹងបំប្លែងជាសំឡេងដោយស្វ័យប្រវត្តិ។</i>',
        parse_mode='HTML',
        message_effect_id="5104841245755180586",
        reply_markup=KEYBOARD
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    user = update.effective_user
    if is_new_user(user.id):
        mark_user_known(user.id)
        asyncio.create_task(notify_admin_new_user(context.bot, user))

    text = update.message.text

    if text == "👨 សំឡេងប្រុស":
        set_gender(update.effective_user.id, "male")
        await update.message.reply_text(
            '<tg-emoji emoji-id="5805174945138872447">✅</tg-emoji> <b>បានប្តូរទៅ 👨 សំឡេងប្រុស</b>',
            parse_mode='HTML',
            reply_to_message_id=update.message.message_id,
            reply_markup=KEYBOARD
        )
        return

    if text == "👩 សំឡេងស្រី":
        set_gender(update.effective_user.id, "female")
        await update.message.reply_text(
            '<tg-emoji emoji-id="5805174945138872447">✅</tg-emoji> <b>បានប្តូរទៅ 👩 សំឡេងស្រី</b>',
            parse_mode='HTML',
            reply_to_message_id=update.message.message_id,
            reply_markup=KEYBOARD
        )
        return

    text = text.strip()

    # Segment text by language (handles single and mixed-language texts)
    segments = segment_text(text)
    is_mixed = len(segments) > 1

    gender = get_gender(update.effective_user.id)
    vm = MALE_VOICES if gender == "male" else FEMALE_VOICES

    if is_mixed:
        cache_key = f"mixed:{gender}:{text}"
    else:
        lang = segments[0][1]
        voice = vm.get(lang) or vm.get('en')
        cache_key = f"{voice}:{text}"

    cached_file_id = _cache_get(cache_key)

    logging.info(f"Segments: {[(c[:12]+'…' if len(c)>12 else c, l) for c,l in segments]} | Cache: {'HIT' if cached_file_id else 'MISS'}")

    try:
        if cached_file_id:
            await update.message.reply_voice(
                voice=cached_file_id,
                reply_markup=KEYBOARD,
            )
        else:
            asyncio.create_task(
                context.bot.send_chat_action(
                    update.effective_chat.id,
                    constants.ChatAction.RECORD_VOICE
                )
            )
            if is_mixed:
                audio_buf = await synthesize_mixed(segments, vm)
            else:
                audio_buf = await synthesize_to_bytes(text, voice, lang=lang)

            msg = await update.message.reply_voice(
                voice=audio_buf,
                reply_markup=KEYBOARD,
            )
            _cache_set(cache_key, msg.voice.file_id)
    except Exception as e:
        logging.error(f"Error synthesizing voice: {e}")
        await update.message.reply_text(
            "⚠️ មានបញ្ហាក្នុងការបង្កើតសំឡេង។ សូមព្យាយាមម្តងទៀត។",
            reply_markup=KEYBOARD,
        )

def create_app():
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    request = HTTPXRequest(
        connection_pool_size=32,
        read_timeout=60,
        write_timeout=60,
        connect_timeout=5,
        http_version="2",
    )
    application = (
        ApplicationBuilder()
        .token(token)
        .request(request)
        .concurrent_updates(True)
        .build()
    )
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_error_handler(error_handler)
    return application

if __name__ == "__main__":
    create_app().run_polling(drop_pending_updates=True, timeout=30)
