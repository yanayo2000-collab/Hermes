from __future__ import annotations

import re
from dataclasses import dataclass, asdict
from typing import Any, Dict, Optional


@dataclass
class NativeOcrParseResult:
    screen_type: str
    raw_text: str
    sid: Optional[str] = None
    profile_id: Optional[str] = None
    invite_code: Optional[str] = None  # backward-compatible alias
    guild_invite_code: Optional[str] = None
    person_code: Optional[str] = None
    agency_name: Optional[str] = None
    guild_name: Optional[str] = None
    username: Optional[str] = None

    @property
    def account_id(self) -> Optional[str]:
        return self.sid or self.profile_id

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["account_id"] = self.account_id
        return data


SID_PATTERNS = [
    r"(?:^|\n)\s*SID\s*(?:Saya|kamu|anda)?\s*[:：]?\s*(\d{6,12})\b",
    r"(?:^|\n)\s*(?:Meu\s+SID|My\s+SID)\s*[:：]?\s*(\d{6,12})\b",
]
PROFILE_ID_PATTERNS = [
    r"(?:^|\n)\s*ID\s*[:：]\s*(\d{6,12})\b",
]
GUILD_INVITE_CODE_PATTERNS = [
    r"(?:^|\n)\s*Kode\s+Undangan\s*[:：]?\s*([A-Z0-9]{4,10})\b",
]
PERSON_CODE_PATTERNS = [
    r"(?:^|\n)\s*kode\s+gabung\s+agensi\s*[:：]?\s*([A-Z0-9]{4,10})\b",
    r"(?:^|\n)\s*(?:C[oó]digo\s+da\s+pessoa|Person\s+Code)\s*[:：]?\s*([A-Z0-9]{4,10})\b",
    r"(?:^|\n)\s*kode\s+gabung\s*\n\s*agensi\s*\n\s*([A-Z0-9]{4,10})\b",
    r"(?:^|\n)\s*kode\s+gabung\s*\n\s*([A-Z0-9]{4,10})\s*\n\s*agensi\b",
]
AGENCY_PATTERNS = [
    r"Agensi\s+saya\s*[:：]?\s*([^\n]{2,40})",
    r"(?:Minha\s+ag[êe]ncia|My\s+Agency)\s*[:：]?\s*([^\n]{2,40})",
]
GUILD_PATTERNS = [
    r"Nama\s+Guild\s*[:：]?\s*([^\n]{2,40})",
    r"NamaGuild\s*[:：]?\s*([^\n]{2,40})",
]
USERNAME_PATTERNS = [
    r"(?:^|\n)([A-Z][a-z]+\s+[A-Z][a-z]+)(?:\n|$)",
]


UNJOINED_AGENCY_PATTERNS = [
    r"未加入公会",
    r"未加入机构",
    r"belum\s+gabung\s+agensi",
    r"belum\s+join\s+agensi",
    r"not\s+joined\s+agency",
    r"ainda\s+n[aã]o\s+entrou",
]


NOT_JOINED_VALUES = {
    "ainda não entrou",
    "ainda nao entrou",
    "not joined yet",
    "belum gabung",
    "belum masuk",
}


def _clean_text(text: str) -> str:
    text = str(text or "")
    text = text.replace("\r", "\n")
    text = text.replace("\u00a0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n+", "\n", text)
    return text.strip()


def _collapse_split_ocr_tokens(text: str) -> str:
    text = str(text or "")
    replacements = {
        r"A\s*g\s*e\s*n\s*s\s*i\s+s\s*a\s*y\s*a": "Agensi saya",
        r"N\s*a\s*m\s*a\s+G\s*u\s*i\s*l\s*d": "Nama Guild",
        r"M\s*i\s*n\s*h\s*a\s+A\s*g\s*[êe]\s*n\s*c\s*i\s*a": "Minha Agência",
    }
    for pattern, repl in replacements.items():
        text = re.sub(pattern, repl, text, flags=re.IGNORECASE)
    text = re.sub(r"\b([A-Za-z]{1,3})\s+([A-Za-z]{2,})(-\d+)\b", lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}", text)
    return text


def _search_first(text: str, patterns: list[str], *, flags: int = re.IGNORECASE) -> Optional[str]:
    for pattern in patterns:
        match = re.search(pattern, text, flags=flags)
        if match:
            return match.group(1).strip()
    return None


def _normalize_code(value: Optional[str]) -> Optional[str]:
    return value.upper() if value else None


def infer_screen_type(raw_text: str) -> str:
    text = _clean_text(raw_text)
    lowered = text.lower()
    compact = lowered.replace(" ", "")
    if "nama guild" in lowered or "selamat datang" in lowered or "kamuberhasilbergabung" in compact:
        return "invite_success_toast"
    if "kode undangan" in lowered or "isi kode undangan" in lowered:
        return "invite_code_form"
    if (
        "agensi saya" in lowered
        or "gabung agensi" in lowered
        or "minha agencia" in lowered
        or "minha agência" in lowered
        or "entrar na agencia" in lowered
        or "entrar na agência" in lowered
    ):
        return "agency_info_card"
    if re.search(r"\bID\s*[:：]\s*\d{6,12}\b", text) and (
        "fãs" in lowered or "amigos" in lowered or "seguir" in lowered or "fas" in lowered
    ):
        return "profile_page"
    return "unknown"


def parse_native_ocr_text(raw_text: str) -> NativeOcrParseResult:
    text = _collapse_split_ocr_tokens(_clean_text(raw_text))
    screen_type = infer_screen_type(text)
    sid = _search_first(text, SID_PATTERNS)
    guild_invite_code = _normalize_code(_search_first(text, GUILD_INVITE_CODE_PATTERNS, flags=re.IGNORECASE))
    person_code = _normalize_code(_search_first(text, PERSON_CODE_PATTERNS, flags=re.IGNORECASE))
    agency_name = _search_first(text, AGENCY_PATTERNS, flags=re.IGNORECASE)
    guild_name = _search_first(text, GUILD_PATTERNS, flags=re.IGNORECASE)
    username = _search_first(text, USERNAME_PATTERNS, flags=0)
    if username and username.lower() in {
        "gabung agensi",
        "kode undangan",
        "amigos fas",
        "agencia meu",
        "meu sid",
        "minha agencia",
    }:
        username = None

    profile_id = _search_first(text, PROFILE_ID_PATTERNS, flags=re.IGNORECASE)
    if not sid and profile_id:
        sid = profile_id

    invite_code = person_code or guild_invite_code

    return NativeOcrParseResult(
        screen_type=screen_type,
        raw_text=text,
        sid=sid,
        profile_id=profile_id,
        invite_code=invite_code,
        guild_invite_code=guild_invite_code,
        person_code=person_code,
        agency_name=agency_name,
        guild_name=guild_name,
        username=username,
    )


def _is_unjoined_agency_name(value: Optional[str]) -> bool:
    text = _clean_text(value or "")
    if not text:
        return False
    for pattern in UNJOINED_AGENCY_PATTERNS:
        if re.search(pattern, text, flags=re.IGNORECASE):
            return True
    return False


def normalize_native_ocr_fields(raw_text: str) -> Dict[str, Any]:
    result = parse_native_ocr_text(raw_text)
    data = result.to_dict()

    agency_value = (result.agency_name or "").strip().lower()
    guild_value = (result.guild_name or "").strip().lower()
    agency_joined = True
    if agency_value in NOT_JOINED_VALUES or guild_value in NOT_JOINED_VALUES:
        agency_joined = False
    if _is_unjoined_agency_name(result.agency_name):
        data["agency_name"] = None
        agency_joined = False
    if _is_unjoined_agency_name(result.guild_name):
        data["guild_name"] = None
        agency_joined = False

    data["agency_joined"] = agency_joined
    data["registration_group"] = None
    data["evidence"] = {
        "matched_sid": bool(result.sid),
        "matched_profile_id": bool(result.profile_id),
        "matched_invite_code": bool(result.invite_code),
        "matched_guild_invite_code": bool(result.guild_invite_code),
        "matched_person_code": bool(result.person_code),
        "matched_agency_name": bool(result.agency_name),
        "matched_guild_name": bool(result.guild_name),
        "matched_unjoined_agency_state": not agency_joined,
    }
    return data
