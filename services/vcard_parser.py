"""
VCF / vCard Parser
==================
Parses one or multiple vCard blocks from a raw bytes payload (UTF-8 or latin-1).
Returns a list of dicts:  {"full_name", "phone", "email", "company"}
Only `phone` is guaranteed non-None for a usable contact.
"""
from __future__ import annotations
import re
from typing import Optional

# Regex helpers
_TEL_RE      = re.compile(r"^TEL[^:]*:(.+)$", re.IGNORECASE)
_EMAIL_RE    = re.compile(r"^EMAIL[^:]*:(.+)$", re.IGNORECASE)
_FN_RE       = re.compile(r"^FN[^:]*:(.+)$", re.IGNORECASE)
_N_RE        = re.compile(r"^N[^:]*:(.+)$", re.IGNORECASE)
_ORG_RE      = re.compile(r"^ORG[^:]*:(.+)$", re.IGNORECASE)


def _clean_phone(raw: str) -> str:
    """Strip everything except digits and a leading +."""
    raw = raw.strip()
    plus = raw.startswith("+")
    digits = re.sub(r"\D", "", raw)
    return ("+" + digits) if plus else digits


def _parse_n(value: str) -> str:
    """
    N property: Last;First;Middle;Prefix;Suffix
    Reconstructs a best-effort full name.
    """
    parts = [p.strip() for p in value.split(";")]
    # parts[0]=last, parts[1]=first, parts[2]=middle
    name_parts = []
    if len(parts) > 1 and parts[1]:   # First
        name_parts.append(parts[1])
    if len(parts) > 2 and parts[2]:   # Middle
        name_parts.append(parts[2])
    if parts[0]:                       # Last
        name_parts.append(parts[0])
    return " ".join(name_parts).strip()


def _split_blocks(raw_text: str) -> list[str]:
    """Split a potentially multi-vCard stream into individual card strings."""
    blocks: list[str] = []
    current_lines: list[str] = []
    inside = False

    for line in raw_text.splitlines():
        stripped = line.strip()
        if stripped.upper() == "BEGIN:VCARD":
            inside = True
            current_lines = [stripped]
        elif stripped.upper() == "END:VCARD":
            if inside:
                current_lines.append(stripped)
                blocks.append("\n".join(current_lines))
                current_lines = []
                inside = False
        elif inside:
            # Handle vCard line folding (lines starting with whitespace)
            if line.startswith((" ", "\t")) and current_lines:
                current_lines[-1] += stripped
            else:
                current_lines.append(stripped)

    return blocks


def _parse_single_block(block: str) -> list[dict]:
    """Parse a single BEGIN:VCARD … END:VCARD block.
    Returns a list of contacts if multiple phone numbers are present.
    """
    full_name: Optional[str] = None
    phones:    list[str]     = []
    email:     Optional[str] = None
    company:   Optional[str] = None

    for line in block.splitlines():
        line = line.strip()
        if not line:
            continue

        fn_m = _FN_RE.match(line)
        if fn_m:
            full_name = fn_m.group(1).strip()
            continue

        n_m = _N_RE.match(line)
        if n_m and not full_name:
            # Only use N as a fallback if FN is absent
            full_name = _parse_n(n_m.group(1))
            continue

        tel_m = _TEL_RE.match(line)
        if tel_m:
            clean = _clean_phone(tel_m.group(1))
            if len(clean.lstrip("+")) >= 7:  # at least 7 raw digits
                phones.append(clean)
            continue

        email_m = _EMAIL_RE.match(line)
        if email_m and not email:
            email = email_m.group(1).strip().lower()
            continue

        org_m = _ORG_RE.match(line)
        if org_m and not company:
            # ORG may have sub-values separated by ;
            company = org_m.group(1).split(";")[0].strip()
            continue

    if not phones:
        return [{
            "full_name": full_name or None,
            "phone":     None,
            "email":     email,
            "company":   company,
        }]

    # Yield a separate contact record for every phone number found
    return [{
        "full_name": full_name or None,
        "phone":     p,
        "email":     email,
        "company":   company,
    } for p in phones]


def parse_vcf(content: bytes) -> list[dict]:
    """
    Parse raw VCF bytes.  Returns a list of contact dicts:
        {"full_name": str|None, "phone": str|None, "email": str|None, "company": str|None}
    Contacts whose phone is None (or too short) will be filtered by the caller.
    """
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = content.decode("latin-1", errors="replace")

    text = text.lstrip("\ufeff")
    blocks = _split_blocks(text)
    contacts = []
    for b in blocks:
        contacts.extend(_parse_single_block(b))
    return contacts
