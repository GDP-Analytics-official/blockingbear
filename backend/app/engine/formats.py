"""Whole candidates and local validators for multilingual printed formats.

Regexes establish boundaries; parsers check structure. None of these checks
claims that an address, account or document was actually issued.
"""

from datetime import date
import re
import unicodedata

from . import lexicon
from .text_patterns import HSPACE, WORD

# A left boundary prevents restarting at each character of a long non-match.
# SMTPUTF8 names and ASCII atext (including apostrophes) are accepted. Quoted
# local parts and address literals are intentionally outside this extractor.
_LOCAL = WORD + r".!#$%&'*+/=?^`{|}~\-"
EMAIL_RX = re.compile(rf"(?<![{_LOCAL}@])[{_LOCAL}]+@[^\s<>\"@,;:()\[\]{{}}]+")


def domain_ok(host):
    try:
        host = unicodedata.normalize("NFC", host).encode("idna").decode("ascii")
    except UnicodeError:
        return False
    if len(host) > 253:
        return False
    labels = host.split(".")
    return len(labels) >= 2 and all(
        re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
        for label in labels) and bool(re.fullmatch(r"[A-Za-z]{2,63}|xn--[A-Za-z0-9-]+", labels[-1]))


def email_ok(value):
    local, host = value.rsplit("@", 1)
    return (len(local.encode("utf-8")) <= 64 and not local.startswith(".")
            and not local.endswith(".") and ".." not in local and domain_ok(host))


# Consume the complete dotted token once. The TLD decision happens afterwards,
# so a known suffix inside example.com.invalid cannot become a partial URL.
URL_RX = re.compile(
    rf"(?<![{WORD}.@-])(?:(?:https?|ftp)://[^\s<>\"']+"
    rf"|(?:[{WORD}-]+\.)+[{WORD}-]+(?:[/?#][^\s<>\"']*)?)", re.I)


def trim_url(value):
    while value:
        if value[-1] in ".,;:!?…»›\"'":
            value = value[:-1]
        elif value[-1] in ")]}":
            opening = {")": "(", "]": "[", "}": "{"}[value[-1]]
            if value.count(value[-1]) <= value.count(opening):
                break
            value = value[:-1]
        else:
            break
    return value


def url_ok(value):
    if re.match(r"(?:https?|ftp)://", value, re.I):
        return True
    host = re.split(r"[/?#]", value, maxsplit=1)[0]
    if not domain_ok(host):
        return False
    if host.lower().startswith("www."):
        return True
    labels = host.split(".")
    if labels[-1].lower() in lexicon.PUBLIC_TLD_LOOSE:
        return True
    # Preserve the deliberate negative controls for short abbreviations and
    # uppercase two-letter suffixes. Explicit www./schemes remain unambiguous.
    suffix = next((t for t in sorted(lexicon.PUBLIC_TLD_2ND, key=len, reverse=True)
                   if host.endswith("." + t)), labels[-1])
    stem = host[:-(len(suffix) + 1)].rsplit(".", 1)[-1]
    return (suffix in lexicon.PUBLIC_TLD_STRICT + lexicon.PUBLIC_TLD_2ND
            and len(stem) >= 3)


_CURRENCY = r"(?:€|£|\$|EUR|USD|GBP|CHF|CAD|AUD|euros?|dollars?|pounds?|sterline)"
_NUMBER = r"[+\-−]?[0-9]+(?:[.,'’ \t\u00a0\u202f][0-9]+)*"
AMOUNT_RX = re.compile(
    rf"(?<![{WORD}.,])(?:{_CURRENCY}{HSPACE}*{_NUMBER}|{_NUMBER}{HSPACE}*{_CURRENCY})"
    rf"(?![{WORD}]|[.,'’][0-9])", re.I)
_AMOUNT_NUMBER = re.compile(_NUMBER)


def amount_ok(value):
    number = _AMOUNT_NUMBER.search(value).group().lstrip("+-−")
    number = re.sub(HSPACE, " ", number).replace("’", "'")
    # The last point/comma is a decimal separator only for one/two decimals.
    decimal = re.search(r"([.,])([0-9]{1,2})$", number)
    integer = number[:decimal.start()] if decimal else number
    groups = re.split(r"([., '])", integer)
    if len(groups) == 1:
        return True
    separators = groups[1::2]
    return (len(set(separators)) == 1 and 1 <= len(groups[0]) <= 3
            and all(len(group) == 3 for group in groups[2::2])
            and (not decimal or decimal.group(1) not in separators))


DATE_RX = re.compile(
    r"(?<![\w/.-])(?P<a>[0-9]{1,4})(?P<sep>[/.-])(?P<b>[0-9]{1,2})"
    r"(?P=sep)(?P<c>[0-9]{2,4})"
    r"(?:[ T](?P<h>[0-9]{1,2})[:.](?P<m>[0-9]{2})(?::(?P<s>[0-9]{2}))?)?"
    r"(?![\w]|[/.-][0-9])")


def date_ok(value):
    match = DATE_RX.fullmatch(value)
    if not match:
        return False
    a, b, c = (int(match[x]) for x in ("a", "b", "c"))
    if len(match["a"]) == 4:
        candidates = [(a, b, c)]
    elif len(match["c"]) == 4:
        candidates = [(c, b, a), (c, a, b)]
    else:
        return False
    if match["h"] is not None and (int(match["h"]) > 23 or int(match["m"]) > 59
                                    or int(match["s"] or 0) > 59):
        return False
    for year, month, day in candidates:
        if not 1900 <= year <= 2099:
            continue
        try:
            date(year, month, day)
            return True
        except ValueError:
            pass
    return False


# Full month names only: short forms such as "mar" overlap ordinary prose.
_MONTHS = (
    "gennaio febbraio marzo aprile maggio giugno luglio agosto settembre ottobre novembre dicembre",
    "january february march april may june july august september october november december",
    "janvier février mars avril mai juin juillet août septembre octobre novembre décembre",
    "januar februar märz april mai juni juli august september oktober november dezember",
    "enero febrero marzo abril mayo junio julio agosto septiembre octubre noviembre diciembre",
    "januari februari maart april mei juni juli augustus september oktober november december",
)
MONTH_NUMBERS = {name: number for names in _MONTHS
                 for number, name in enumerate(names.split(), 1)}
_MONTH = "(?:" + "|".join(sorted(MONTH_NUMBERS, key=len, reverse=True)) + ")"
NAMED_DATE_RX = re.compile(
    rf"(?<![{WORD}])(?:(?P<day>[0-9]{{1,2}})\.?{HSPACE}+(?:de{HSPACE}+)?"
    rf"(?P<month>{_MONTH}){HSPACE}+(?:de{HSPACE}+)?(?P<year>[0-9]{{4}})"
    rf"|(?P<usmonth>{_MONTH}){HSPACE}+(?P<usday>[0-9]{{1,2}}),?{HSPACE}+"
    rf"(?P<usyear>[0-9]{{4}}))(?![{WORD}])", re.I)


def named_date_ok(value):
    m = NAMED_DATE_RX.fullmatch(value)
    if not m:
        return False
    year = int(m['year'] or m['usyear'])
    month = MONTH_NUMBERS[(m['month'] or m['usmonth']).lower()]
    day = int(m['day'] or m['usday'])
    try:
        date(year, month, day)
        return 1900 <= year <= 2099
    except ValueError:
        return False


# Printed common formats, gated by an explicit vehicle-plate cue. This is
# recognition, not an assertion of issuance; historic/special plates vary.
# Common formats only; this does not cover every historic or special series.
_PLATE_CUE = (r"targa|(?:number|licen[cs]e|registration)[ -]plate|vehicle registration"
              r"|plaque(?: d[’']immatriculation)?|immatriculation|kennzeichen"
              r"|nummernschild|matr[íi]cula|kenteken(?:nummer)?")
_NL_PLATES = ("LL-DD-DD", "DD-DD-LL", "DD-LL-DD", "LL-DD-LL", "LL-LL-DD",
              "DD-LL-LL", "DD-LLL-D", "D-LLL-DD", "LL-DDD-L", "L-DDD-LL",
              "LLL-DD-L", "L-DD-LLL", "D-LL-DDD", "DDD-LL-D")
_NL_PLATE = "(?:" + "|".join(p.replace("L", "[A-Z]").replace("D", "[0-9]")
                              for p in _NL_PLATES) + ")"
PLATE_RX = re.compile(
    rf"(?<![{WORD}])(?:{_PLATE_CUE})\.?{HSPACE}*[:=]?{HSPACE}*"
    rf"(?P<value>[A-Z]{{2}}[0-9]{{2}}{HSPACE}+[A-Z]{{3}}"
    rf"|[A-ZÄÖÜ]{{1,3}}-[A-Z]{{1,2}}{HSPACE}+[1-9][0-9]{{0,3}}"
    rf"|[0-9]{{4}}{HSPACE}+[BCDFGHJKLMNPRSTVWXYZ]{{3}}|{_NL_PLATE})"
    rf"(?![{WORD}-])", re.I)
