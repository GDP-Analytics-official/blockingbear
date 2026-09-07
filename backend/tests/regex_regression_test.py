"""Deterministic regex regressions: Unicode spans and lossless document redaction.

Run a copy of this suite in an isolated backend container. No model, network,
production data or credentials are required; all fixtures are synthetic.
"""
import io
import os
from pathlib import Path
import re
import sys
import tempfile
import unicodedata
import unittest
from unittest.mock import patch
from xml.sax.saxutils import escape
import zipfile

_tmp = tempfile.TemporaryDirectory(prefix="blockingbear-regex-tests-")
os.environ["BLOCKINGBEAR_DATA_DIR"] = _tmp.name
os.environ["DATABASE_URL"] = ""
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import fitz
from app.engine import core, detectors, docx, pdf_export, pptx, txt, xlsx
from app.engine.text_patterns import contains_literal
from app.engine.xlsx_table import PlaceholderAllocator, column_uniques


class RegexOnly(core.PiiEngine):
    def detect_model(self, text, ctl=None):
        return []


def package(kind, text):
    """Small XML packages exercise the real ZIP/run replacement path."""
    value = escape(text)
    parts = {
        "docx": ("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>' + value + '</w:t></w:r></w:p></w:body></w:document>'),
        "pptx": ("ppt/slides/slide1.xml", '<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><p:cSld><p:spTree><p:sp><p:txBody><a:p><a:r><a:t>' + value + '</a:t></a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld></p:sld>'),
        "xlsx": ("xl/worksheets/sheet1.xml", '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>' + value + '</t></is></c></row></sheetData></worksheet>'),
    }
    name, xml = parts[kind]
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr(name, xml)
        if kind == "pptx":
            archive.writestr("ppt/presentation.xml", '<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"/>')
        elif kind == "xlsx":
            archive.writestr("xl/workbook.xml", '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"/>')
    return out.getvalue()


def pdf_bytes(text):
    with fitz.open() as document:
        page = document.new_page()
        page.insert_text((72, 72), text)
        return document.tobytes()


class RegexRegression(unittest.TestCase):
    def setUp(self):
        self.engine = RegexOnly("/tmp/no-regex-test-model")

    def assertSpan(self, text, value, label):
        found = [(e["label"], e["start"], e["end"]) for e in detectors.detect_regex(text)]
        start = text.index(value)
        self.assertIn((label, start, start + len(value)), found, text)

    def assertNoLabel(self, text, label):
        self.assertNotIn(label, [e['label'] for e in detectors.detect_regex(text)], text)

    def test_six_languages_preserve_unicode_offsets(self):
        cases = [
            ('password è Estate2024!', 'Estate2024!', 'PASSWORD'),
            ('password is Winter2024!', 'Winter2024!', 'PASSWORD'),
            ('mot de passe: Été2024!', 'Été2024!', 'PASSWORD'),
            ('Passwort: Frühling2024!', 'Frühling2024!', 'PASSWORD'),
            ('contraseña: Otoño2024!', 'Otoño2024!', 'PASSWORD'),
            ('wachtwoord: Zomer2024!', 'Zomer2024!', 'PASSWORD'),
        ] + [(cue + ': AB12CD34', 'AB12CD34', 'DEVICE_ID') for cue in (
            'numero di serie', 'serial number', 'numéro de série',
            'Seriennummer', 'número de serie', 'serienummer')]
        transforms = [lambda s: s, lambda s: s.replace(' ', '\u00a0'),
                      lambda s: s.replace(' ', '\u202f'), lambda s: unicodedata.normalize('NFD', s)]
        for text, value, label in cases:
            for transform in transforms:
                raw, expected = transform('😀 ' + text), transform(value)
                with self.subTest(text=raw):
                    self.assertSpan(raw, expected, label)
                    result = self.engine.analyze(raw)
                    self.assertIn(expected, result['mapping'].values())
                    self.assertEqual(core.decode_text(result['anonymized_text'], result['mapping'])[0], raw)

    def test_credential_values_are_complete(self):
        cases = [('OTP: 123456AB', '123456AB', 'PASSWORD'),
                 ('password: abcDEF123?', 'abcDEF123?', 'PASSWORD'),
                 ('password: "abc\\"DEF123"', 'abc\\"DEF123', 'PASSWORD'),
                 ('password: "required"', 'required', 'PASSWORD'),
                 ('password: " My paSs! "', ' My paSs! ', 'PASSWORD'),
                 ('password: ' + 'Ab1!' * 45, 'Ab1!' * 45, 'PASSWORD'),
                 ('sshpass -p ' + 'Ab1!' * 45, 'Ab1!' * 45, 'PASSWORD'),
                 ('mysql -p' + 'Ab1!' * 45, 'Ab1!' * 45, 'PASSWORD')]
        for cue in ('passphrase', 'phrase de passe', 'wachtwoordzin', 'frase de paso'):
            cases.append((cue + ': correct horse battery staple', 'correct horse battery staple', 'PASSWORD'))
        for text, value, label in cases:
            with self.subTest(text=text):
                self.assertSpan(text, value, label)
                result = self.engine.analyze(text)
                self.assertIn(value, result['mapping'].values())
                self.assertEqual(core.decode_text(result['anonymized_text'], result['mapping'])[0], text)
        self.assertNoLabel('PIN: 1234AB', 'PASSWORD')
        for value in ('AB12CD34', 'EF56GH78'):
            self.assertSpan('Recovery codes: AB12CD34, EF56GH78', value, 'SECRET')
        for cue in ('Recovery codes', 'Codici di recupero', 'Codes de récupération',
                    'Wiederherstellungscodes', 'Códigos de recuperación', 'Herstelcodes'):
            text = cue + ': 1234-5678, 2345-6789, 3456-7890'
            for code in ('1234-5678', '2345-6789', '3456-7890'):
                self.assertSpan(text, code, 'SECRET')

    def test_prose_is_not_a_credential(self):
        for text in ('La password è obbligatoria.', 'The password is required.',
                     'Le mot de passe est obligatoire.', 'Das Passwort ist erforderlich.',
                     'La contraseña es obligatoria.', 'Het wachtwoord is verplicht.'):
            self.assertNoLabel(text, 'PASSWORD')

    def test_formats_accept_whole_values(self):
        cases = {
            'AMOUNT': ['€ 1234,56', '1234,56 EUR', 'EUR 1,234.56', '£1,234.56', '$1,234.56',
                       '1\u202f234,56 €', 'CHF 1’234.50'],
            'DATE': ['31/12/2026', '12/31/2026', '2026-12-31', '29/02/2024',
                     '31 dicembre 2026', 'December 31, 2026', '31 décembre 2026',
                     '31. Dezember 2026', '31 de diciembre de 2026', '31 december 2026'],
            'EMAIL': ["o'connor@example.com", 'élise@example.fr', 'a@bücher.de'],
            'URL': ['bücher.de', 'https://example.com/user_(alice)', 'example.com?x=1'],
        }
        for label, values in cases.items():
            for value in values:
                self.assertSpan('Value: ' + value + '.', value, label)
        for label, values in {
            'AMOUNT': ['€ 1.23.45', '€ 12,3456', 'europe 1234'],
            'DATE': ['31/02/2026', '29/02/2025', '31/12-2026', '31/12/2026 99:99', '31 février 2026'],
            'EMAIL': ['alice@example.com123', 'alice@-example.com', 'alice@example..com'],
            'URL': ['example.com.invalid', 'S.r.l.', 'p.iva', 'EXAMPLE.DE', 'x.de'],
        }.items():
            for value in values:
                self.assertNoLabel(value, label)

    def test_printed_vat_and_national_identifiers(self):
        for text, value in [('VAT GB 980 7806 84', 'GB 980 7806 84'),
                            ('TVA FR 40 303 265 045', 'FR 40 303 265 045'),
                            ('USt-IdNr. DE 136 695 976', 'DE 136 695 976'),
                            ('P.IVA IT 007 431 10157', 'IT 007 431 10157'),
                            ('vat de136695976', 'de136695976')]:
            self.assertSpan(text, value, 'PIVA')
            for space in ('\u00a0', '\u202f'):
                raw, expected = text.replace(' ', space), value.replace(' ', space)
                self.assertSpan(raw, expected, 'PIVA')
                start = raw.index(expected)
                corrected = core.post_check([{'label': 'PIVA', 'start': start + 2,
                                              'end': len(raw), 'validated': False}], raw)
                self.assertEqual(raw[corrected[0]['start']:corrected[0]['end']], expected)
        for value in ('12345678z', '12345678\u00a0Z'):
            self.assertSpan('DNI ' + value, value, 'CF')
        self.assertSpan('SSN: 123-45-6789', '123-45-6789', 'CF')
        self.assertNoLabel('Invoice: 123-45-6789', 'CF')
        for value in ('000-45-6789', '666-45-6789', '900-45-6789', '123-00-6789', '123-45-0000'):
            self.assertNoLabel('SSN: ' + value, 'CF')
        self.assertFalse(detectors.piva_ok('00000000000'))
        self.assertFalse(detectors.luhn_ok('0000000000000000'))
        self.assertFalse(detectors.iban_ok('ZZ00' + '0' * 20))

    def test_context_is_multilingual(self):
        for cue in ('Prot. n.', 'Case No.', 'Aktenzeichen', 'Référence dossier', 'Expediente', 'Zaaknummer'):
            text = cue + ' 1234/2026'
            self.assertSpan(text, text, 'DOCID')
        for text, value in [('number plate: AB12 CDE', 'AB12 CDE'),
                            ('Kennzeichen: B-AB 1234', 'B-AB 1234'),
                            ('matrícula: 1234 BCD', '1234 BCD'),
                            ('kenteken: AB-12-CD', 'AB-12-CD')]:
            self.assertSpan(text, value, 'TARGA')
            self.assertNoLabel('Item: ' + value, 'TARGA')
        for cue in ('versione', 'version', 'versión', 'versie'):
            self.assertNoLabel(cue + ' 2.4.1.0', 'IP_ADDRESS')
        for cue in ('hash della password', 'password hash', 'hash du mot de passe',
                    'passwort-hash', 'hash de la contraseña', 'hash van het wachtwoord'):
            self.assertSpan(cue + ': ' + '0123456789abcdef' * 2, '0123456789abcdef' * 2, 'PASSWORD_HASH')
        self.assertSpan('2001:db8::1/999', '2001:db8::1', 'IP_ADDRESS')
        self.assertSpan('2001:db8::1/128', '2001:db8::1/128', 'IP_ADDRESS')
        for extension in ('ext.', 'interno', 'poste', 'Durchwahl', 'anexo', 'toestel'):
            value = '+44 20 7946 0123 ' + extension + ' 456'
            self.assertSpan('Phone: ' + value, value, 'TELEPHONENUM')

    def test_exact_mapping_and_single_pass_restore(self):
        text = 'PaSs123! pass123! Päss123? -pAbCd123'
        mapping = {'[PASSWORD_1]': 'PaSs123!', '[PASSWORD_2]': 'Päss123?', '[PASSWORD_3]': 'AbCd123'}
        output, report = txt.redact_txt(text.encode(), mapping)
        self.assertEqual(output.decode(), '[PASSWORD_1] pass123! [PASSWORD_2] -p[PASSWORD_3]')
        self.assertEqual(report['residual'], [])
        self.assertEqual(core.decode_text(output.decode(), mapping)[0], text)
        self.assertEqual(core.decode_text('[FULLNAME_1]', {'[FULLNAME_1]': '[EMAIL_1]', '[EMAIL_1]': 'x@example.com'}), ('[EMAIL_1]', 1))
        self.assertEqual(core.decode_text('[123_1][_TAG_1]', {'[123_1]': 'one', '[_TAG_1]': 'two'}), ('onetwo', 2))
        value = 'https://example.com/user_(alice)'
        result = self.engine.analyze(value)
        self.assertEqual(result['mapping'], {'[URL_1]': value})
        output, report = txt.redact_txt(value.encode(), result['mapping'])
        self.assertEqual(core.decode_text(output.decode(), result['mapping'])[0], value)

    def test_secret_symbols_and_spaces_remain_data(self):
        from app import chat_anonymization as chat
        for value in ('!@#$', ' My paSs! ', 'PaSs123!'):
            text = 'password: "' + value + '"'
            result = self.engine.analyze(text)
            self.assertEqual(list(result['mapping'].values()), [value])
            output, report = txt.redact_txt(text.encode(), result['mapping'])
            self.assertEqual(output.decode(), 'password: "[PASSWORD_1]"')
            self.assertEqual(report['residual'], [])
            self.assertEqual(core.decode_text(output.decode(), result['mapping'])[0], text)
            self.assertEqual(chat._surface_core('PASSWORD', value), value)
            self.assertFalse(chat._junk_value('PASSWORD', value))
        for title in ('Mr.', 'Mme', 'Herr', 'Sr.', 'Dhr.'):
            self.assertEqual(chat._surface_core('FULLNAME', title + ' Alex Smith'),
                             chat._surface_core('FULLNAME', 'Alex Smith'))
        for suffix in ('GmbH', 'B.V.', 'Inc.'):
            self.assertEqual(chat._surface_core('ORG', 'Acme Labs ' + suffix),
                             chat._surface_core('ORG', 'Acme Labs'))

    def test_unicode_custom_terms(self):
        for value, variants in [('Straße', ['Straße', 'STRASSE']), ('José', ['José', 'Jose\u0301']),
                                ('Müller', ['Müller', 'Mu\u0308ller'])]:
            for variant in variants:
                result = self.engine.analyze(variant, custom_terms=[{'text': value, 'tag': 'CUSTOM'}])
                self.assertEqual(list(result['mapping'].values()), [variant])
                output, report = txt.redact_txt(variant.encode(), {'[CUSTOM_1]': value})
                self.assertEqual(output.decode(), '[CUSTOM_1]')
                self.assertEqual(report['residual'], [])
        self.assertFalse(contains_literal('14990.90', '4990', '[BUILDINGNUM_1]'))
        self.assertFalse(contains_literal('Joanna', 'Ann', '[FULLNAME_1]'))
        self.assertIsNone(pdf_export._value_pattern('secrete\u0301', '[PASSWORD_1]').search('secrete\u0301ly'))

    def test_ooxml_exact_redaction_and_independent_residuals(self):
        text = 'Straße PaSs123! pass123! Jose\u0301'
        mapping = {'[ADDRESS_1]': 'Straße', '[PASSWORD_1]': 'PaSs123!', '[FULLNAME_1]': 'José'}
        for kind, module in [('docx', docx), ('pptx', pptx), ('xlsx', xlsx)]:
            data = package(kind, text)
            with self.subTest(kind=kind):
                output, report = getattr(module, 'redact_' + kind)(data, mapping)
                extracted = module.extract_text(output)
                self.assertNotIn('Straße', extracted)
                self.assertNotIn('PaSs123!', extracted)
                self.assertIn('pass123!', extracted)
                self.assertEqual(report['occurrences'], 3)
                self.assertEqual(report['residual'], [])
                with patch.object(module, '_value_pattern', return_value=None):
                    self.assertEqual(set(module._verify_residuals(data, list(mapping.items()))), set(mapping))
        with patch.object(txt, '_value_pattern', return_value=re.compile(r'(?!x)x')):
            _, report = txt.redact_txt(text.encode(), mapping)
            self.assertEqual(set(report['residual']), set(mapping))

    def test_spreadsheet_allocation_preserves_exact_values(self):
        mapping = {'[PASSWORD_1]': 'PaSs123!', '[FULLNAME_1]': 'José'}
        allocator = PlaceholderAllocator(mapping)
        self.assertEqual(allocator.get('PASSWORD', 'pass123!'), ('[PASSWORD_2]', True))
        self.assertEqual(allocator.get('FULLNAME', 'Jose\u0301'), ('[FULLNAME_1]', False))
        self.assertEqual(allocator.forget('[PASSWORD_2]'), 'pass123!')
        self.assertEqual(allocator.get('PASSWORD', 'pass123!'), ('[PASSWORD_3]', True))
        rows = [(1, [(0, 'PaSs123!', True)]), (2, [(0, 'pass123!', True)])]
        self.assertEqual(column_uniques(rows, {0: 'Password'}), {0: ['PaSs123!', 'pass123!']})
        data = package('xlsx', 'PaSs123! pass123!')
        output, report = xlsx.redact_xlsx(data, {'[PASSWORD_1]': 'PaSs123!'}, exact_phs={'[PASSWORD_1]'})
        self.assertIn('[PASSWORD_1] pass123!', xlsx.extract_text(output))
        self.assertEqual(report['residual'], [])

    def test_pdf_redaction_and_restoration(self):
        text = 'Straße\nPaSs123!\npass123!'
        mapping = {'[ADDRESS_1]': 'Straße', '[PASSWORD_1]': 'PaSs123!'}
        data = pdf_bytes(text)
        output, report = pdf_export.redact_pdf(data, mapping)
        with fitz.open(stream=output, filetype='pdf') as document:
            extracted = ''.join(page.get_text() for page in document)
        self.assertNotIn('Straße', extracted)
        self.assertNotIn('PaSs123!', extracted)
        self.assertIn('pass123!', extracted)
        self.assertEqual(report['residual'], [])
        restored, _ = pdf_export.restore_pdf(output, mapping)
        with fitz.open(stream=restored, filetype='pdf') as document:
            extracted = ''.join(page.get_text() for page in document)
        for value in ('Straße', 'PaSs123!', 'pass123!'):
            self.assertIn(value, extracted)
        with patch.object(pdf_export, '_value_pattern', return_value=None):
            self.assertEqual(set(pdf_export._verify_residuals(data, list(mapping.items()))), set(mapping))
        historical = {'[123_1]': 'First', '[_TAG_1]': 'Second'}
        restored, report = pdf_export.restore_pdf(pdf_bytes('[123_1]\n[_TAG_1]'), historical)
        with fitz.open(stream=restored, filetype='pdf') as document:
            extracted = ''.join(page.get_text() for page in document)
        self.assertIn('First', extracted)
        self.assertIn('Second', extracted)

    def test_long_near_misses_have_no_partial_urls(self):
        # Correctness on the adversarial family; timing is measured externally.
        self.assertNoLabel('a.' * 4000 + 'invalid', 'URL')
        self.assertNoLabel('a' * 8000, 'EMAIL')


if __name__ == '__main__':
    unittest.main()
