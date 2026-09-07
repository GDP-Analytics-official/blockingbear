"""Workbook names, reference-safe renaming and reversible neutral identifiers.

Restoration templates contain placeholders, never their protected values. They
are stored as hidden string-valued defined names, which spreadsheet editors
can preserve during an ordinary save. If an editor drops that information the
neutral name remains; no original value is embedded in an opaque identifier.
"""
import hashlib
import posixpath
import re
import xml.etree.ElementTree as ET

from .docx import _part_namespaces, _serialize_part
from .pdf_export import sub_placeholders
from .text_patterns import PLACEHOLDER_RE

X = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_MARKER = "_BB_RESTORE_"
_TOKEN = re.compile(r"BB_[SN]_[0-9a-f]{20}$")
_LEX = re.compile(r'''"(?:[^"]|"")*"|'(?:[^']|'')*'|(?:[^\W\d]|[_\\])[\w.\\]*|\d+(?:\.\d*)?(?:[eE][+-]?\d+)?|.''', re.S)
_WORD = re.compile(r"(?:[^\W\d]|[_\\])[\w.\\]*$", re.UNICODE)
_FORMULAS = {"f", "formula", "formula1", "formula2", "definedName",
             "calculatedColumnFormula", "totalsRowFormula"}


class WorkbookNameError(ValueError):
    pass


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _scope(el):
    scope = el.get("localSheetId")
    return int(scope) if scope is not None else None


def _literal(text):
    return '"' + text.replace('"', '""') + '"'


def _unliteral(text):
    if re.fullmatch(r'"(?:[^"]|"")*"', text or ""):
        return text[1:-1].replace('""', '"')
    return None


def _workbook_fields(el):
    """Text-bearing fields, excluding validated schema control values.

    A1/R1C1 here selects the formula reference syntax; it is not cell data.
    Do not skip unknown values or the same strings in user metadata/cells.
    """
    for attr, value in el.attrib.items():
        if el.tag == X + "calcPr" and attr == "refMode" and value in {"A1", "R1C1"}:
            continue
        yield attr, value
    yield "text", el.text or ""


def workbook_surfaces(data, full=False):
    """The same workbook surfaces feed detection and residual verification.

    Names include all worksheets and chart sheets, regardless of visibility.
    Full mode also covers additional workbook text/attributes so an unsupported
    metadata field cannot silently pass the privacy boundary.
    """
    root = ET.fromstring(data)
    out = []
    for el in root.iter():
        tag = _local(el.tag)
        if full:
            out.extend(value for _, value in _workbook_fields(el) if value.strip())
        elif tag == "sheet":
            out.append(el.get("name", ""))
        elif tag == "definedName" and not el.get("name", "").startswith(_MARKER):
            out.append(el.get("name", ""))
            if el.text:
                out.append(el.text)
    return out


def reference_surfaces(root):
    """References outside workbook.xml, including external workbook caches.

    These are inspected even when a reference type cannot be rewritten. Keep
    numeric layout fields (row indices, dimensions, style IDs) out of the corpus.
    """
    fields = {"worksheetSource": {"sheet", "name"}, "sheetName": {"val"},
              "externalName": {"name"}, "sheetPr": {"codeName"},
              "table": {"name", "displayName"}, "tableColumn": {"name"}}
    out = []
    for el in root.iter():
        tag = _local(el.tag)
        if tag in _FORMULAS and el.text:
            out.append(el.text)
        out.extend(el.get(attr) for attr in fields.get(tag, ()) if el.get(attr))
    return out


def _part_scopes(parts, root):
    rels = ET.fromstring(parts.get("xl/_rels/workbook.xml.rels", b"<Relationships/>"))
    targets = {e.get("Id"): posixpath.normpath(posixpath.join("xl", e.get("Target", "")))
               if not e.get("Target", "").startswith("/") else e.get("Target").lstrip("/")
               for e in rels}
    return {targets[e.get(R + "id")]: i
            for i, e in enumerate(root.findall(X + "sheets/" + X + "sheet"))
            if e.get(R + "id") in targets}


def _rewrite_formula(text, sheets, names, sheet_ids, scope=None):
    """Tokenize references; string literals and structured table refs stay data.

    Local names override workbook names. Qualified local names and quoted / 3-D
    sheet references are resolved explicitly. External workbook references are
    left intact and are subject to residual verification.
    """
    tokens = _LEX.findall(text)
    significant = [i for i, t in enumerate(tokens) if not t.isspace()]
    prev = {i: significant[n - 1] if n else None for n, i in enumerate(significant)}
    nex = {i: significant[n + 1] if n + 1 < len(significant) else None
           for n, i in enumerate(significant)}
    original = list(tokens)
    depth, consumed = 0, set()
    for i in significant:
        if i in consumed:
            continue
        token = original[i]
        if token == "[":
            depth += 1
            continue
        if token == "]":
            depth = max(0, depth - 1)
            continue
        if depth or token.startswith('"'):
            continue
        ni, pi = nex[i], prev[i]
        nt = original[ni] if ni is not None else ""
        pt = original[pi] if pi is not None else ""
        is_word = bool(_WORD.fullmatch(token))
        if not (is_word or token.startswith("'")):
            continue
        if (sheets or names) and is_word and nt == "(" and token.upper().split(".")[-1] in {"INDIRECT", "EVALUATE"}:
            raise WorkbookNameError(
                "Cannot safely rename Excel names used by dynamic INDIRECT/EVALUATE references.")
        # First endpoint in an unquoted 3-D reference (Sheet1:Sheet3!A1).
        three_d = (nt == ":" and nex.get(ni) is not None
                   and nex.get(nex[ni]) is not None
                   and original[nex[nex[ni]]] == "!")
        if three_d:
            end_i = nex[ni]
            first, last = token, original[end_i]
            changed = [sheets.get(s.casefold(), s) for s in (first, last)]
            if changed != [first, last]:
                tokens[i] = "'" + ":".join(changed).replace("'", "''") + "'"
                tokens[ni] = tokens[end_i] = ""
                consumed.update((ni, end_i))
            continue
        if nt == "!" or three_d:
            raw = token[1:-1].replace("''", "'") if token.startswith("'") else token
            if "[" in raw or pt == "]":
                continue
            endpoints = raw.split(":")
            changed = [sheets.get(s.casefold(), s) for s in endpoints]
            if changed != endpoints:
                tokens[i] = "'" + ":".join(changed).replace("'", "''") + "'"
            continue
        if not is_word or nt in {"(", "["} or pt in {"#", "]"}:
            continue
        context = scope
        if pt == "!":
            sheet_i = prev[pi]
            raw = original[sheet_i] if sheet_i is not None else ""
            if raw.startswith("'"):
                raw = raw[1:-1].replace("''", "'")
            if "[" in raw or (sheet_i is not None and prev[sheet_i] is not None
                              and original[prev[sheet_i]] == "]"):
                continue
            context = sheet_ids.get(raw.casefold())
        key = token.casefold()
        replacement = names.get((context, key), names.get((None, key)))
        if replacement is not None:
            tokens[i] = replacement
        elif context is None and any(k == key and s is not None for s, k in names):
            raise WorkbookNameError("Cannot resolve the scope of an Excel defined-name reference.")
    return "".join(tokens)


def _rewrite_parts(parts, root, sheets, names, before_ids, index=None, hit_counts=None):
    scopes = _part_scopes(parts, root)
    changed = {}
    for part, data in parts.items():
        if not part.startswith("xl/") or not part.endswith((".xml", ".rels")):
            continue
        tree = root if part == "xl/workbook.xml" else ET.fromstring(data)
        touched = part == "xl/workbook.xml"
        for el in tree.iter():
            tag = _local(el.tag)
            if tag == "table" and index is not None:
                for attr in ("name", "displayName"):
                    if index.matches(el.get(attr, "")):
                        raise WorkbookNameError(
                            f"Cannot safely rewrite the protected Excel table identifier in {part} ({attr}).")
            if tag == "definedName" and el.get("name", "").startswith(_MARKER):
                continue
            scope = _scope(el) if tag == "definedName" else scopes.get(part)
            if tag in _FORMULAS and el.text:
                old = el.text
                el.text = _rewrite_formula(old, sheets, names, before_ids, scope)
                if tag == "definedName" and index is not None:
                    # Constants in named formulas are data. Quote them using Excel
                    # escaping after replacement, rather than corrupting formulas.
                    def replace_literal(match):
                        value, counts = index.replace(_unliteral(match.group()))
                        if hit_counts is not None:
                            for ph, n in counts.items():
                                hit_counts[ph] = hit_counts.get(ph, 0) + n
                        return _literal(value)
                    el.text = re.sub(r'"(?:[^"]|"")*"', replace_literal, el.text)
                touched |= old != el.text
            source_scope = before_ids.get(el.get("sheet", "").casefold())
            for attr, val in list(el.attrib.items()):
                new = val
                if tag == "worksheetSource" and attr == "sheet":
                    new = sheets.get(val.casefold(), val)
                elif tag == "worksheetSource" and attr == "name":
                    new = names.get((source_scope, val.casefold()),
                                    names.get((None, val.casefold()), val))
                elif attr in {"formula", "location"} or (tag == "Relationship" and attr == "Target" and val.startswith("#")):
                    new = _rewrite_formula(val, sheets, names, before_ids, scope)
                if new != val:
                    el.set(attr, new)
                    touched = True
        if touched:
            changed[part] = _serialize_part(tree, _part_namespaces(data))
    return changed


def redact_names(parts, index):
    """Return changed XML parts and placeholder occurrence counts."""
    root = ET.fromstring(parts["xl/workbook.xml"])
    sheet_nodes = root.findall(X + "sheets/" + X + "sheet")
    before_ids = {el.get("name", "").casefold(): i for i, el in enumerate(sheet_nodes)}
    definitions = root.find(X + "definedNames")
    dn_nodes = list(definitions) if definitions is not None else []
    used = {el.get("name", "").casefold() for el in sheet_nodes + dn_nodes}
    sheets, names, templates, hits = {}, {}, [], {}
    for kind, nodes in (("S", sheet_nodes), ("N", dn_nodes)):
        for el in nodes:
            old = el.get("name", "")
            if old.startswith(_MARKER):
                continue
            template, counts = index.replace(old)
            if not counts:
                # Identity local entries preserve shadowing of renamed globals.
                if kind == "N":
                    names[(_scope(el), old.casefold())] = old
                continue
            for ph, n in counts.items():
                hits[ph] = hits.get(ph, 0) + n
            scope = _scope(el) if kind == "N" else None
            nonce = 0
            while True:
                digest = hashlib.sha256(f"{kind}|{scope}|{template}|{nonce}".encode()).hexdigest()[:20]
                token = f"BB_{kind}_{digest}"
                if token.casefold() not in used and (_MARKER + token).casefold() not in used:
                    break
                nonce += 1
            used.update((token.casefold(), (_MARKER + token).casefold()))
            el.set("name", token)
            if kind == "S":
                sheets[old.casefold()] = token
            else:
                names[(scope, old.casefold())] = token
            templates.append((token, template, scope))
    if templates:
        if definitions is None:
            definitions = ET.Element(X + "definedNames")
            # definedNames follows externalReferences and precedes calcPr.
            after = {"calcPr", "oleSize", "customWorkbookViews", "pivotCaches",
                     "smartTagPr", "smartTagTypes", "webPublishing", "fileRecoveryPr",
                     "webPublishObjects", "extLst"}
            pos = next((i for i, e in enumerate(root) if _local(e.tag) in after), len(root))
            root.insert(pos, definitions)
        for token, template, scope in templates:
            attrs = {"name": _MARKER + token, "hidden": "1"}
            if scope is not None:
                attrs["localSheetId"] = str(scope)
            ET.SubElement(definitions, X + "definedName", attrs).text = _literal(template)
    # Identity entries matter only when at least one defined name is renamed.
    if not any(old != new.casefold() for (_scope_id, old), new in names.items()):
        names = {}
    changed = _rewrite_parts(parts, root, sheets, names, before_ids, index, hits)
    checked = ET.fromstring(changed.get("xl/workbook.xml", parts["xl/workbook.xml"]))
    for el in checked.iter():
        for attr, value in _workbook_fields(el):
            remaining = index.matches(value)
            if remaining:
                ph = remaining[0][0]
                raise WorkbookNameError(
                    f"Protected data remains in Excel workbook metadata: {_local(el.tag)}/{attr} ({ph}).")
    return changed, hits


def restore_names(parts, mapping):
    """Restore complete templates atomically, preserving all name references."""
    root = ET.fromstring(parts["xl/workbook.xml"])
    definitions = root.find(X + "definedNames")
    if definitions is None:
        return {}, 0
    sheet_nodes = root.findall(X + "sheets/" + X + "sheet")
    before_ids = {e.get("name", "").casefold(): i for i, e in enumerate(sheet_nodes)}
    sheets, names, total = {}, {}, 0
    for marker in list(definitions):
        mname = marker.get("name", "")
        if not mname.startswith(_MARKER):
            continue
        token = mname[len(_MARKER):]
        template = _unliteral(marker.text)
        if not _TOKEN.fullmatch(token) or template is None:
            continue
        placeholders = PLACEHOLDER_RE.findall(template)
        if not placeholders or any(ph not in mapping for ph in placeholders):
            continue
        restored, count = sub_placeholders(template, mapping)
        scope = _scope(marker)
        is_sheet = token.startswith("BB_S_")
        nodes = sheet_nodes if is_sheet else list(definitions)
        target = next((e for e in nodes if e.get("name") == token
                       and (is_sheet or _scope(e) == scope)), None)
        if target is None:
            definitions.remove(marker)
            continue
        valid = (bool(restored) and len(restored) <= 31
                 and not re.search(r"[\[\]:*?/\\\x00-\x1f]", restored)
                 and not restored.startswith("'") and not restored.endswith("'")) if is_sheet else (
                    bool(_WORD.fullmatch(restored)) and len(restored) <= 255)
        collision = any(e is not target and e.get("name", "").casefold() == restored.casefold()
                        and (is_sheet or _scope(e) == scope) for e in nodes)
        if not valid or collision:
            raise WorkbookNameError("Restored Excel name is invalid or duplicates an existing name.")
        target.set("name", restored)
        if is_sheet:
            sheets[token.casefold()] = restored
        else:
            names[(scope, token.casefold())] = restored
        definitions.remove(marker)
        total += count
    if not total:
        return {}, 0
    changed = _rewrite_parts(parts, root, sheets, names, before_ids)
    return changed, total


def restore_formula_literals(parts, mapping):
    """Excel quotes are distinct from XML escaping; restore string tokens first."""
    changed, total = {}, 0
    for part, data in parts.items():
        if not part.startswith("xl/") or not part.endswith(".xml"):
            continue
        root = ET.fromstring(data)
        touched = False
        for el in root.iter():
            if _local(el.tag) not in _FORMULAS or not el.text:
                continue
            def restore_literal(match):
                nonlocal total, touched
                value, n = sub_placeholders(_unliteral(match.group()), mapping)
                total += n
                touched |= bool(n)
                return _literal(value)
            el.text = re.sub(r'"(?:[^"]|"")*"', restore_literal, el.text)
        if touched:
            changed[part] = _serialize_part(root, _part_namespaces(data))
    return changed, total
