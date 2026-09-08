"""Page-only signature detection through projects and chat, using isolated data."""

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

_tmp = tempfile.TemporaryDirectory(prefix='blockingbear-signature-flow-')
os.environ['BLOCKINGBEAR_DATA_DIR'] = _tmp.name
os.environ['DATABASE_URL'] = ''
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import fitz
from app import chat_anonymization as ca, project_files, settings_store
from app.auth import seed_admin
from app.config import MODEL_DIR
from app.db import Attachment, Conversation, Project, init_db
from app.engine import image_ocr
from app.engine.core import PiiEngine
from pdf_signatures_test import document, NoOCR


def page_only_detector(im):
    if im.size == (200, 100):
        return []
    rect = fitz.Rect(105, 305, 285, 395) * fitz.Matrix(im.width / 400, im.height / 600)
    return [{'b': list(rect), 's': .9}]


def main():
    Session = init_db()
    engine = PiiEngine(MODEL_DIR)
    source = document()
    checks = 0

    def check(condition, description):
        nonlocal checks
        assert condition, description
        checks += 1
        print('PASS ' + description, flush=True)

    with patch.object(image_ocr, '_get_ocr', return_value=NoOCR()), \
            patch.object(image_ocr, 'detect_regions', side_effect=page_only_detector):
        with Session() as session:
            seed_admin(session)
            settings_store.set_anon_defaults(session, [], [])
            project = Project(owner_id=1, name='Synthetic signatures', anonymized=1)
            session.add(project)
            session.commit()
            pf = project_files.process_upload(source, 'signatures.pdf', project.id,
                                              engine, session, ocr=True)
            cache = json.loads(project_files.ocr_cache_path(pf).read_text())
            desc = project_files.descriptor(session, project, pf)
            ph = next(k for k in desc['mapping'] if k.startswith('[SIGNATURE_'))
            check(any(im.get('pdf_signature') for im in cache['images']),
                  'Project persists the page signature entry')
            boxes = [b for group in desc['anonymized_boxes'].values() for b in group if b['ph'] == ph]
            check(len(boxes) == 1, 'Project preview has one signature box')
            with fitz.open(project_files.protected_path(pf)) as doc:
                check('Native header stays selectable.' in doc[0].get_text(),
                      'Project output preserves native text')
            project_files.deanonymize(session, project, pf, placeholder=ph)
            desc = project_files.descriptor(session, project, pf)
            check(not any(b['ph'] == ph for group in desc['anonymized_boxes'].values() for b in group),
                  'Project restoration removes the page signature box')
            pf = project_files.reprocess_ocr({'project_id': project.id, 'file_id': pf.id}, engine, session)
            desc = project_files.descriptor(session, project, pf)
            check(any(b['ph'].startswith('[SIGNATURE_') for group in desc['anonymized_boxes'].values() for b in group),
                  'Reprocess OCR runs the page detector again')

        ca._base_engine = lambda: engine
        with Session() as session:
            conv = Conversation(owner_id=1, anonymized=1)
            session.add(conv)
            session.flush()
            att = Attachment(conv_id=conv.id, direction='in', filename='signatures.pdf',
                             display_filename='signatures.pdf', mime='application/pdf',
                             size=len(source), anonymization_status='pending')
            session.add(att)
            session.flush()
            folder = Path(_tmp.name) / 'chats' / conv.id
            folder.mkdir(parents=True)
            original = folder / (att.id + '_signatures.pdf')
            original.write_bytes(source)
            att.original_path = str(original)
            cid, aid = conv.id, att.id
            session.commit()
        _, _, descriptions = ca.anonymize_turn(cid, 'Documento sintetico.', [aid], ocr=True)
        check(descriptions[0]['anonymization_status'] == 'protected', 'Chat processes the attachment')
        with Session() as session:
            att = session.get(Attachment, aid)
            cache = ca.load_att_ocr_cache(att)
            entry = next(im for im in cache['images'] if im.get('pdf_signature'))
            check(len(entry['plan']) == 1, 'Chat persists one page signature plan')
            ph = entry['plan'][0]['ph']
            check(ph in image_ocr.unreadable_mapping(cache), 'Chat adds the signature to its local mapping')
            with fitz.open(att.protected_path) as doc:
                check(ph in doc[0].get_text(), 'Chat output contains the signature replacement')
                check('Native header stays selectable.' in doc[0].get_text(), 'Chat preserves native text')
            image_ocr.exclude_local(cache, placeholder=ph)
            check(ph not in image_ocr.unreadable_mapping(cache), 'Chat local restoration excludes the signature')
    print(f'{checks} PASS, 0 FAIL')


if __name__ == '__main__':
    main()
