"""Real OCR/model integration for tiled PDFs in projects and chat.

Only synthetic, in-memory fixtures and a disposable SQLite database are used.
Run: python tests/pdf_image_regions_flow_test.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

_tmp = tempfile.TemporaryDirectory(prefix='blockingbear-region-flow-')
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
from pdf_image_regions_test import document


def main():
    Session = init_db()
    engine = PiiEngine(MODEL_DIR)
    source = document(rows=24, vertical=True, shuffle=True, logo=True)
    checks = 0

    def check(ok, description):
        nonlocal checks
        assert ok, description
        checks += 1
        print('PASS '+description, flush=True)

    with Session() as session:
        seed_admin(session)
        settings_store.set_anon_defaults(session, [], [])
        project = Project(owner_id=1, name='Synthetic tiled card', anonymized=1)
        session.add(project)
        session.commit()
        pf = project_files.process_upload(source, 'synthetic.pdf', project.id,
                                          engine, session, ocr=False)
        check(not project_files.ocr_cache_path(pf).exists(), 'OCR-off upload has no cache')
        oldrev = pf.rev
        pf = project_files.reprocess_ocr({'project_id':project.id,'file_id':pf.id}, engine, session)
        check(pf.rev > oldrev, 'Reprocessing advances the preview revision')
        cache = json.loads(project_files.ocr_cache_path(pf).read_text())
        check(sum(bool(i.get('pdf_region')) for i in cache['images']) == 1,
              'Region metadata persists through the project processing flow')
        desc = project_files.descriptor(session, project, pf)
        ph = next((k for k,v in desc['mapping'].items() if v.upper() == 'MARIO ROSSI'), None)
        check(bool(ph), 'Real PII model recognizes the synthetic name')
        check(any(b['ph']==ph for group in desc['anonymized_boxes'].values() for b in group),
              'Project preview includes the image name boxes')
        protected = project_files.protected_path(pf).read_bytes()
        with fitz.open(stream=protected,filetype='pdf') as doc:
            check('Native header stays selectable.' in doc[0].get_text(), 'Native header retained')
            check('Native footer stays selectable.' in doc[0].get_text(), 'Native footer retained')
        region = next(i for i in cache['images'] if i.get('pdf_region'))
        for data in (source, protected):
            text = image_ocr.text_in_rect_cache(data,0,region['pdf_region']['rect'],cache)
            check('MARIO ROSSI' in text.upper(), 'Cached selection works on original/protected preview')
        project_files.deanonymize(session,project,pf,placeholder=ph)
        desc = project_files.descriptor(session,project,pf)
        check(not any(b['ph']==ph for group in desc['anonymized_boxes'].values() for b in group),
              'Restoring the name removes all of its tile masks')
        project_files.anonymize_text(session,project,pf,'MARIO ROSSI')
        desc = project_files.descriptor(session,project,pf)
        check(any(b['ph']==ph for group in desc['anonymized_boxes'].values() for b in group),
              'Manual re-anonymization restores the tile masks from the saved cache')

    ca._base_engine = lambda: engine
    with Session() as session:
        conv = Conversation(owner_id=1,anonymized=1)
        session.add(conv)
        session.flush()
        att = Attachment(conv_id=conv.id,direction='in',filename='synthetic.pdf',
                         display_filename='synthetic.pdf',mime='application/pdf',
                         size=len(source),anonymization_status='pending')
        session.add(att)
        session.flush()
        folder = Path(_tmp.name)/'chats'/conv.id
        folder.mkdir(parents=True)
        original = folder/(att.id+'_synthetic.pdf')
        original.write_bytes(source)
        att.original_path = str(original)
        cid, aid = conv.id, att.id
        session.commit()
    _, _, descriptions = ca.anonymize_turn(cid,'Documento sintetico.',[aid],ocr=True)
    check(descriptions[0]['anonymization_status']=='protected','Chat attachment is processed')
    with Session() as session:
        att = session.get(Attachment,aid)
        cache = ca.load_att_ocr_cache(att)
        check(any(i.get('pdf_region') for i in cache['images']),'Chat persists region metadata')
        check(any(i['plan'] for i in cache['images'] if i.get('pdf_region')),
              'Chat has image redaction plans for the composed region')
        check(Path(att.protected_path).read_bytes()!=source,'Chat writes a redacted PDF')
        with fitz.open(att.protected_path) as doc:
            check('Native header stays selectable.' in doc[0].get_text(),'Chat preserves native text')
    print(f'{checks} PASS, 0 FAIL')


if __name__ == '__main__':
    main()
