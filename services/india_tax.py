"""Small, deterministic helpers shared by native GST invoice validation and rendering."""

import re


GST_STATE_NAMES = {
    "01": "Jammu and Kashmir",
    "02": "Himachal Pradesh",
    "03": "Punjab",
    "04": "Chandigarh",
    "05": "Uttarakhand",
    "06": "Haryana",
    "07": "Delhi",
    "08": "Rajasthan",
    "09": "Uttar Pradesh",
    "10": "Bihar",
    "11": "Sikkim",
    "12": "Arunachal Pradesh",
    "13": "Nagaland",
    "14": "Manipur",
    "15": "Mizoram",
    "16": "Tripura",
    "17": "Meghalaya",
    "18": "Assam",
    "19": "West Bengal",
    "20": "Jharkhand",
    "21": "Odisha",
    "22": "Chhattisgarh",
    "23": "Madhya Pradesh",
    "24": "Gujarat",
    "25": "Daman and Diu",
    "26": "Dadra and Nagar Haveli and Daman and Diu",
    "27": "Maharashtra",
    "28": "Andhra Pradesh (Old)",
    "29": "Karnataka",
    "30": "Goa",
    "31": "Lakshadweep",
    "32": "Kerala",
    "33": "Tamil Nadu",
    "34": "Puducherry",
    "35": "Andaman and Nicobar Islands",
    "36": "Telangana",
    "37": "Andhra Pradesh",
    "38": "Ladakh",
    "97": "Other Territory",
}

_GSTIN_PATTERN = re.compile(r"\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]")
_BASE36 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def gst_state_name(code: str) -> str:
    """Return the statutory display name for a supported GST state code."""
    try:
        return GST_STATE_NAMES[str(code).zfill(2)]
    except KeyError as exc:
        raise ValueError("Choose a valid Indian GST state code") from exc


def gstin_check_digit(first_fourteen: str) -> str:
    """Calculate the ISO 7064/Luhn-mod-36 GSTIN check character."""
    value = first_fourteen.upper()
    if len(value) != 14 or any(character not in _BASE36 for character in value):
        raise ValueError("GSTIN checksum input must contain 14 base-36 characters")
    factor = 2
    total = 0
    for character in reversed(value):
        addend = factor * _BASE36.index(character)
        total += addend // 36 + addend % 36
        factor = 1 if factor == 2 else 2
    return _BASE36[(36 - total % 36) % 36]


def is_valid_gstin(value: str) -> bool:
    gstin = str(value or "").strip().upper()
    return bool(_GSTIN_PATTERN.fullmatch(gstin)) and gstin[-1] == gstin_check_digit(gstin[:14])
