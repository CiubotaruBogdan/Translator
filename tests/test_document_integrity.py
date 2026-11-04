"""Real OOXML/PDF/OCR regressions; no translation model or network required."""
import io
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

import docx
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.shared import Inches
from fastapi import UploadFile, HTTPException
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import server


def hyperlink(paragraph, label):
    element = OxmlElement('w:hyperlink')
    element.set(qn('r:id'), paragraph.part.relate_to('https://example.com/reference', RT.HYPERLINK, is_external=True))
    run = OxmlElement('w:r')
    text = OxmlElement('w:t'); text.text = label
    run.append(text); element.append(run); paragraph._p.append(element)


class DocumentIntegrityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.uploads = self.root / 'uploads'; self.uploads.mkdir()
        self.outputs = self.root / 'outputs'; self.outputs.mkdir()
        for name, value in [('UPLOAD_DIR', self.uploads), ('OUTPUT_DIR', self.outputs)]:
            patcher = patch.object(server, name, value); patcher.start(); self.addCleanup(patcher.stop)

    def save(self, document, name='source.docx'):
        path = self.uploads / name; document.save(path); return path

    def apply(self, path, translations):
        result = self.outputs / 'result.docx'
        server.apply_translations_to_docx(path, server.extract_docx_segments(path), translations, result)
        return docx.Document(result)

    def test_inline_picture_in_text_run_survives_translation(self):
        image = self.root / 'picture.png'; Image.new('RGB', (20, 20), 'blue').save(image)
        document = docx.Document(); run = document.add_paragraph().add_run('Caption')
        run.add_picture(str(image), width=Inches(.3))
        translated = self.apply(self.save(document), ['Legendă tradusă'])
        self.assertEqual(translated.paragraphs[0].text, 'Legendă tradusă')
        self.assertEqual(len(translated.inline_shapes), 1)

    def test_page_and_column_breaks_survive_text_replacement(self):
        from docx.enum.text import WD_BREAK
        for kind, name in [(WD_BREAK.PAGE, 'page'), (WD_BREAK.COLUMN, 'column')]:
            with self.subTest(kind=name):
                document = docx.Document(); run = document.add_paragraph().add_run('Text')
                run.add_break(kind)
                translated = self.apply(self.save(document), ['Tradus'])
                breaks = translated.paragraphs[0]._p.xpath('.//w:br')
                self.assertEqual([br.get(qn('w:type')) for br in breaks], [name])

    def test_hyperlink_text_is_sent_and_link_target_survives(self):
        document = docx.Document(); para = document.add_paragraph('Visit ')
        hyperlink(para, 'IMPORTANT LINK'); para.add_run(' now').bold = True
        path = self.save(document)
        segments = server.extract_docx_segments(path)
        self.assertIn('IMPORTANT LINK', segments[0]['text'])
        translated = self.apply(path, ['⟦0⟧Vizitează ⟦1⟧LEGĂTURA IMPORTANTĂ⟦2⟧ acum'])
        para = translated.paragraphs[0]
        self.assertEqual(para.text, 'Vizitează LEGĂTURA IMPORTANTĂ acum')
        link = para._p.xpath('./w:hyperlink')[0]
        self.assertEqual(para.part.rels[link.get(qn('r:id'))].target_ref, 'https://example.com/reference')
        self.assertTrue(para.runs[-1].bold)

    def test_hyperlink_only_paragraph_keeps_its_url(self):
        document = docx.Document(); hyperlink(document.add_paragraph(), 'Link label')
        translated = self.apply(self.save(document), ['Etichetă tradusă'])
        para = translated.paragraphs[0]
        self.assertEqual(para.text, 'Etichetă tradusă')
        self.assertEqual(len(para._p.xpath('./w:hyperlink')), 1)
        self.assertEqual(para.part.rels[para._p.xpath('./w:hyperlink')[0].get(qn('r:id'))].target_ref, 'https://example.com/reference')

    def test_reordered_markers_follow_ids_and_preserve_word_order(self):
        document = docx.Document(); para = document.add_paragraph('ordinary ')
        para.add_run('bold').bold = True
        translated = self.apply(self.save(document), ['⟦1⟧aldin ⟦0⟧normal'])
        runs = [run for run in translated.paragraphs[0].runs if run.text]
        self.assertEqual(translated.paragraphs[0].text, 'aldin normal')
        self.assertTrue(runs[0].bold)
        self.assertFalse(bool(runs[1].bold))

    def test_reordered_hyperlink_keeps_target_attached_to_link_text(self):
        document = docx.Document(); para = document.add_paragraph('normal ')
        hyperlink(para, 'link')
        translated = self.apply(self.save(document), ['⟦1⟧legătură ⟦0⟧text normal'])
        para = translated.paragraphs[0]
        self.assertEqual(para.text, 'legătură text normal')
        links = [node for node in para._p.xpath('./w:hyperlink') if node.xpath('.//w:t')]
        self.assertEqual(len(links), 1)
        self.assertEqual(''.join(links[0].xpath('.//w:t/text()')), 'legătură ')

    def test_headers_footers_and_their_tables_are_translated_once(self):
        document = docx.Document(); document.add_paragraph('Body')
        section = document.sections[0]
        section.header.paragraphs[0].text = 'Header'
        section.header.add_table(rows=1, cols=1, width=Inches(2)).cell(0, 0).text = 'Header table'
        section.footer.paragraphs[0].text = 'Footer'
        document.add_section()  # Linked header/footer must not become duplicate segments.
        path = self.save(document); segments = server.extract_docx_segments(path)
        self.assertEqual([seg['text'] for seg in segments], ['Body', 'Header', 'Header table', 'Footer'])
        translated = self.apply(path, ['Corp', 'Antet', 'Tabel antet', 'Subsol'])
        self.assertEqual(translated.sections[0].header.paragraphs[0].text, 'Antet')
        self.assertEqual(translated.sections[0].header.tables[0].cell(0, 0).text, 'Tabel antet')
        self.assertEqual(translated.sections[1].footer.paragraphs[0].text, 'Subsol')

    def test_header_alignment_does_not_match_unpaired_body_text(self):
        source = docx.Document(); source.add_paragraph('Source'); source.sections[0].header.paragraphs[0].text = 'Source header'
        target = docx.Document(); target.add_paragraph('Target'); target.add_paragraph('Unpaired'); target.sections[0].header.paragraphs[0].text = 'Target header'
        src, sm = server.extract_review_segments(self.save(source, 'source.docx'))
        tgt, tm = server.extract_review_segments(self.save(target, 'target.docx'))
        aligned, count, mismatch = server.align_review_segments(src, sm, tgt, tm)
        self.assertEqual(aligned, ['Source', '', 'Source header'])
        self.assertEqual(count, 2); self.assertTrue(mismatch)

    async def test_docx_ocr_includes_table_and_header_text(self):
        document = docx.Document(); document.add_table(rows=1, cols=1).cell(0, 0).text = 'TABLE CONTENT'
        document.sections[0].header.paragraphs[0].text = 'HEADER CONTENT'
        stream = tempfile.SpooledTemporaryFile(max_size=1024 * 1024, mode='w+b'); document.save(stream); stream.seek(0)
        self.addCleanup(stream.close)
        with patch.object(server, 'convert_docx_to_pdf_libreoffice', side_effect=RuntimeError('no PDF converter')):
            result = await server.api_ocr(UploadFile(file=stream, filename='table.docx'), 'en')
        self.assertIn('TABLE CONTENT', result['text']); self.assertIn('HEADER CONTENT', result['text'])
        self.assertGreater(result['char_count'], 0)
        self.assertEqual(list(self.uploads.iterdir()), [])
        self.assertTrue((self.outputs / f"ocr_{result['ocr_id']}_result.docx").exists())

    async def test_empty_docx_ocr_is_not_reported_as_success(self):
        stream = tempfile.SpooledTemporaryFile(max_size=1024 * 1024, mode='w+b'); docx.Document().save(stream); stream.seek(0)
        self.addCleanup(stream.close)
        with self.assertRaises(HTTPException) as error:
            await server.api_ocr(UploadFile(file=stream, filename='empty.docx'), 'en')
        self.assertEqual(error.exception.status_code, 400)
        self.assertEqual(list(self.uploads.iterdir()), [])
        self.assertEqual(list(self.outputs.iterdir()), [])

    def test_pdf_review_reuses_retained_converted_docx(self):
        document = docx.Document(); document.add_paragraph('Source body')
        image = self.root / 'picture.png'; Image.new('RGB', (20, 20), 'red').save(image)
        document.add_paragraph().add_run().add_picture(str(image), width=Inches(.2))
        converted = self.save(document, 'job_converted.docx')
        job = server.TranslationJob('job', 'source.pdf', 'en', 'ro', 'model', 'http://unused', '-')
        job.original_docx_path = str(converted)
        job.original_docx_segments = server.extract_docx_segments(converted)
        output, _ = server.build_revised_document(job, ['Corp revizuit'])
        revised = docx.Document(output)
        self.assertEqual(revised.paragraphs[0].text, 'Corp revizuit')
        self.assertEqual(len(revised.inline_shapes), 1)

    def test_missing_retained_pdf_base_fails_instead_of_dropping_layout(self):
        job = server.TranslationJob('job', 'source.pdf', 'en', 'ro', 'model', 'http://unused', '-')
        job.original_docx_path = str(self.uploads / 'missing-converted.docx')
        with self.assertRaisesRegex(FileNotFoundError, 'Documentul de bază'):
            server.build_revised_document(job, ['Tradus'])

    def test_missing_docx_base_does_not_silently_drop_document_objects(self):
        job = server.TranslationJob('job', 'source.docx', 'en', 'ro', 'model', 'http://unused', '-')
        job.original_docx_segments = [{'index': 0, 'type': 'paragraph', 'para_index': 0}]
        with self.assertRaisesRegex(FileNotFoundError, 'Documentul DOCX'):
            server.build_revised_document(job, ['Tradus'])

    def test_unreadable_scanned_page_fails_instead_of_disappearing(self):
        image = Image.new('RGB', (300, 400), 'gray')
        source = self.uploads / 'unreadable.pdf'; image.save(source, format='PDF')
        target = self.outputs / 'unreadable.docx'
        with patch.object(server, '_ocr_pdf_page', return_value=''):
            with self.assertRaisesRegex(ValueError, 'pagina scanată 1'):
                server.prepare_pdf_for_translation(source, target, lang='eng')
        self.assertFalse(target.exists())

    @unittest.skipUnless(server.TESSERACT_AVAILABLE and server.PDF2DOCX_AVAILABLE and shutil.which('pdftoppm'), 'PDF/OCR runtime required')
    def test_mixed_pdf_includes_scanned_page_and_external_review_can_read_scan(self):
        import fitz
        font_path = Path('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf')
        if not font_path.exists():
            self.skipTest('Test font not installed')
        image = Image.new('RGB', (1600, 400), 'white')
        ImageDraw.Draw(image).text((60, 140), 'IMPORTANT SCANNED SECOND PAGE', fill='black',
                                  font=ImageFont.truetype(str(font_path), 55))
        image_stream = io.BytesIO(); image.save(image_stream, format='PNG')
        pdf = fitz.open(); first = pdf.new_page()
        first.insert_text((60, 80), 'This digital page contains more than fifty characters and should be translated.')
        second = pdf.new_page(); second.insert_image(fitz.Rect(20, 20, 575, 820), stream=image_stream.getvalue())
        third = pdf.new_page(); third.insert_text((60, 80), 'Third digital page with a preserved picture.')
        icon = io.BytesIO(); Image.new('RGB', (40, 40), 'blue').save(icon, format='PNG')
        third.insert_image(fitz.Rect(60, 120, 100, 160), stream=icon.getvalue())
        source = self.uploads / 'mixed.pdf'; pdf.save(source); pdf.close()
        converted = self.uploads / 'mixed.docx'
        server.prepare_pdf_for_translation(source, converted, lang='eng')
        texts = '\n'.join(seg['text'] for seg in server.extract_docx_segments(converted))
        self.assertIn('digital page', texts)
        self.assertIn('IMPORTANT SCANNED SECOND PAGE', texts)
        self.assertIn('Third digital page', texts)
        self.assertEqual(len(docx.Document(converted).inline_shapes), 1)
        scan = fitz.open(); page = scan.new_page(); page.insert_image(fitz.Rect(20, 20, 575, 820), stream=image_stream.getvalue())
        scan_path = self.uploads / 'scan.pdf'; scan.save(scan_path); scan.close()
        # Override only the OCR language while exercising actual external-PDF preparation.
        prepare = server.prepare_pdf_for_translation
        with patch.object(server, 'prepare_pdf_for_translation',
                          side_effect=lambda src, dst: prepare(src, dst, lang='eng')):
            reviewed, metadata = server.extract_review_segments(scan_path)
        self.assertIn('IMPORTANT SCANNED SECOND PAGE', '\n'.join(reviewed))
        self.assertTrue(metadata)


if __name__ == '__main__':
    unittest.main()
