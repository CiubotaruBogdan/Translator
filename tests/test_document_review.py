"""Regression checks for revised DOCX contents and failed engine responses.

Run: python -m unittest discover -s tests -v
Network calls are replaced at the engine boundary; DOCX extraction, application,
upload validation, download paths and job state transitions use real code.
"""
import io
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import docx
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from fastapi import HTTPException, UploadFile
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import server


def upload(document, filename):
    data = tempfile.SpooledTemporaryFile(max_size=10 * 1024 * 1024, mode="w+b")
    document.save(data)
    data.seek(0)
    return UploadFile(file=data, filename=filename)


class ReachableSession:
    """An engine accepts readiness requests; generation is mocked separately."""
    status = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def get(self, *args, **kwargs):
        return self

    async def json(self):
        return [{"code": "en"}, {"code": "ro"}]


class DocumentReviewTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.uploads = self.root / "uploads"
        self.outputs = self.root / "outputs"
        self.uploads.mkdir()
        self.outputs.mkdir()
        for name, value in (("UPLOAD_DIR", self.uploads), ("OUTPUT_DIR", self.outputs),
                            ("jobs", {}), ("sse_queues", {})):
            patcher = patch.object(server, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def verify(self, original, translated):
        original_upload = upload(original, "source.docx")
        translated_upload = upload(translated, "translation.docx")
        try:
            response = await server.api_verify_documents(
                SimpleNamespace(client=SimpleNamespace(host="127.0.0.1")),
                original_upload, translated_upload, "en", "ro",
            )
        finally:
            await original_upload.close()
            await translated_upload.close()
        return server.jobs[response["job_id"]], response

    def reviewed(self, job, suggestions=None):
        job.review_status = "completed"
        job.review_results = [
            {"index": i, "suggestion": (suggestions or {}).get(i, "")}
            for i in range(len(job.translated_chunks))
        ]

    async def apply(self, job, **kwargs):
        response = await server.api_review_apply(job.job_id, server.ReviewApplyRequest(**kwargs))
        download = await server.api_review_download(job.job_id)
        self.assertEqual(Path(download.path).name, f"{job.job_id}_rev_{response['filename']}")
        return docx.Document(download.path), response

    async def test_external_review_applies_table_edits_to_translated_document(self):
        original = docx.Document()
        original.add_paragraph("Original paragraph")
        original.sections[0].header.paragraphs[0].text = "SOURCE HEADER"
        original.add_table(rows=1, cols=2).rows[0].cells[0].text = "Original cell"
        translated = docx.Document()
        translated.add_paragraph("Paragraf tradus").runs[0].italic = True
        translated.sections[0].header.paragraphs[0].text = "ANTET TRADUS"
        table = translated.add_table(rows=1, cols=2)
        table.cell(0, 0).text = "Celulă tradusă"
        table.cell(0, 1).text = "Conținut tradus fără pereche"
        table.cell(0, 1).paragraphs[0].runs[0].bold = True
        job, response = await self.verify(original, translated)
        self.assertTrue(response["mismatch"])
        self.assertEqual(response["total"], 2)
        self.reviewed(job)
        revised, applied = await self.apply(job, indices=[1], edits={"1": "Celulă corectată manual"})
        self.assertEqual(applied["applied"], 1)
        self.assertEqual(revised.tables[0].cell(0, 0).text, "Celulă corectată manual")
        self.assertEqual(revised.tables[0].cell(0, 1).text, "Conținut tradus fără pereche")
        self.assertTrue(revised.tables[0].cell(0, 1).paragraphs[0].runs[0].bold)
        self.assertEqual(revised.paragraphs[0].text, "Paragraf tradus")
        self.assertTrue(revised.paragraphs[0].runs[0].italic)
        self.assertEqual(revised.sections[0].header.paragraphs[0].text, "ANTET TRADUS")
        # Revisions are a new file, never an overwrite of either upload.
        unchanged = docx.Document(self.uploads / f"{job.job_id}_translation.docx")
        self.assertEqual(unchanged.tables[0].cell(0, 0).text, "Celulă tradusă")

    async def test_table_only_documents_include_merged_nested_and_multiple_paragraphs(self):
        def make_document(prefix):
            document = docx.Document()
            table = document.add_table(rows=1, cols=2)
            cell = table.cell(0, 0).merge(table.cell(0, 1))
            cell.text = prefix + " first"
            cell.add_paragraph(prefix + " second")
            cell.add_table(rows=1, cols=1).cell(0, 0).text = prefix + " nested"
            return document
        job, response = await self.verify(make_document("Source"), make_document("Tradus"))
        self.assertEqual(response["total"], 3)
        self.assertFalse(response["mismatch"])
        self.reviewed(job, {1: "Al doilea corectat", 2: "Tabel interior corectat"})
        revised, _ = await self.apply(job, indices=[1, 2])
        cell = revised.tables[0].cell(0, 0)
        self.assertEqual(cell.paragraphs[0].text, "Tradus first")
        self.assertEqual(cell.paragraphs[1].text, "Al doilea corectat")
        self.assertEqual(cell.tables[0].cell(0, 0).text, "Tabel interior corectat")

    async def test_normal_translation_review_updates_tables_and_preserves_inline_format(self):
        original = docx.Document()
        original.add_paragraph("Original body")
        cell = original.add_table(rows=1, cols=1).cell(0, 0)
        cell.paragraphs[0].add_run("Ordinary ")
        cell.paragraphs[0].add_run("bold").bold = True
        cell.add_paragraph("Second source paragraph")
        path = self.uploads / "translation_source.docx"
        original.save(path)
        segments = server.extract_docx_segments(path)
        job = server.TranslationJob("translation", "source.docx", "en", "ro", "model", "http://unused", "-")
        job.original_filename = job.filename
        job.original_docx_segments = segments
        job.translated_chunks = ["Corp tradus", "⟦0⟧Normal ⟦1⟧aldin", "Al doilea paragraf"]
        server.jobs[job.job_id] = job
        self.reviewed(job, {1: "Normal corectat aldin"})
        job.review_results[1]["suggestion_raw"] = "⟦0⟧Normal corectat ⟦1⟧aldin"
        revised, _ = await self.apply(job, indices=[1])
        cell = revised.tables[0].cell(0, 0)
        self.assertEqual(revised.paragraphs[0].text, "Corp tradus")
        self.assertEqual(cell.paragraphs[0].text, "Normal corectat aldin")
        self.assertTrue(cell.paragraphs[0].runs[1].bold)
        self.assertEqual(cell.paragraphs[1].text, "Al doilea paragraf")

    async def test_manual_empty_edit_really_clears_table_text(self):
        original = docx.Document()
        original.add_table(rows=1, cols=1).cell(0, 0).text = "Text"
        job, _ = await self.verify(original, original)
        self.reviewed(job)
        revised, result = await self.apply(job, indices=[0], edits={"0": ""})
        self.assertEqual(result["applied"], 1)
        self.assertEqual(revised.tables[0].cell(0, 0).text, "")

    def test_table_with_omitted_leading_cell_keeps_its_structural_locations(self):
        document = docx.Document()
        table = document.add_table(rows=2, cols=2)
        for row_index, row in enumerate(table.rows):
            for cell_index, cell in enumerate(row.cells):
                cell.text = f"row{row_index}-cell{cell_index}"
        first_row = table.rows[0]._tr
        first_row.remove(first_row.tc_lst[0])
        before = OxmlElement("w:gridBefore")
        before.set(qn("w:val"), "1")
        first_row.get_or_add_trPr().append(before)
        path = self.uploads / "omitted.docx"
        document.save(path)
        segments = server.extract_docx_segments(path)
        self.assertEqual(len(segments), 3)
        target = self.outputs / "corrected.docx"
        server.apply_translations_to_docx(path, segments, ["First", "Second", "Third"], target)
        result = docx.Document(target)
        self.assertEqual([cell.text for row in result.tables[0].rows for cell in row.cells],
                         ["First", "Second", "Third"])

    async def test_external_text_review_preserves_unmatched_translated_tail(self):
        original = UploadFile(file=tempfile.SpooledTemporaryFile(mode="w+b"), filename="source.txt")
        translated = UploadFile(file=tempfile.SpooledTemporaryFile(mode="w+b"), filename="translated.txt")
        original.file.write(b"Source paragraph")
        translated.file.write("Paragraf tradus\n\nText suplimentar păstrat".encode())
        original.file.seek(0)
        translated.file.seek(0)
        try:
            response = await server.api_verify_documents(
                SimpleNamespace(client=None), original, translated, "en", "ro")
        finally:
            await original.close()
            await translated.close()
        self.assertTrue(response["mismatch"])
        job = server.jobs[response["job_id"]]
        self.reviewed(job, {0: "Paragraf corectat"})
        await server.api_review_apply(job.job_id, server.ReviewApplyRequest(indices=[0]))
        download = await server.api_review_download(job.job_id)
        self.assertEqual(Path(download.path).read_text(), "Paragraf corectat\n\nText suplimentar păstrat\n\n")

    async def test_invalid_edit_indices_are_rejected(self):
        with self.assertRaises(ValidationError):
            server.ReviewApplyRequest(edits={"not-an-index": "edit"})
        original = docx.Document()
        original.add_paragraph("Text")
        job, _ = await self.verify(original, original)
        self.reviewed(job)
        with self.assertRaises(HTTPException) as error:
            await self.apply(job, apply_all=True, edits={"42": "edit"})
        self.assertEqual(error.exception.status_code, 400)
        self.assertIsNone(job.revised_output_file)

    async def test_unavailable_model_fails_review_instead_of_reporting_completion(self):
        job = server.TranslationJob("review", "text.txt", "en", "ro", "translator", "http://unused", "-")
        job.source_segments = ["Source"]
        job.translated_chunks = ["Traducere"]
        job.review_summary = {"ok": 99}
        job.revised_output_file = "old.docx"
        with patch.object(server.aiohttp, "ClientSession", return_value=ReachableSession()), \
                patch.object(server, "review_segment", AsyncMock(side_effect=RuntimeError("model not found"))):
            await server.run_review(job, "chosen-reviewer", "http://unused")
        self.assertEqual(job.review_status, "failed")
        self.assertIn("model not found", job.review_error)
        self.assertEqual(job.review_model, "chosen-reviewer")
        self.assertEqual(job.review_summary["errors"], 1)
        self.assertIsNone(job.revised_output_file)

    async def test_review_start_reserves_job_and_clears_stale_results_before_background_task(self):
        job = server.TranslationJob("review", "text.txt", "en", "ro", "translator", "http://unused", "-")
        job.status = server.JobStatus.COMPLETED
        job.source_segments = ["Source"]
        job.translated_chunks = ["Traducere"]
        job.review_summary = {"ok": 99}
        job.revised_output_file = "old.docx"
        job.review_results = [{"index": 0}]
        server.jobs[job.job_id] = job
        with patch.object(server.asyncio, "create_task", side_effect=lambda coro: coro.close()):
            response = await server.api_review(job.job_id, server.ReviewRequest(model="chosen-reviewer"))
            self.assertEqual(response["model"], "chosen-reviewer")
            self.assertEqual(job.review_status, "running")
            self.assertIsNone(job.review_summary)
            self.assertIsNone(job.revised_output_file)
            self.assertEqual(job.review_results, [])
            with self.assertRaises(HTTPException) as error:
                await server.api_review(job.job_id, server.ReviewRequest(model="other"))
            self.assertEqual(error.exception.status_code, 409)

    async def test_all_translation_failures_do_not_create_completed_document(self):
        job = server.TranslationJob("failed", "source.txt", "en", "ro", "model", "http://unused", "-",
                                    engine="libretranslate")
        (self.uploads / "failed_source.txt").write_text("The cat is sleeping.")
        with patch.object(server.aiohttp, "ClientSession", return_value=ReachableSession()), \
                patch.object(server, "translate_chunk_libretranslate", AsyncMock(side_effect=RuntimeError("model missing"))):
            await server.run_pipeline(job)
        self.assertEqual(job.status, server.JobStatus.FAILED)
        self.assertEqual(job.failed_chunks, job.total_chunks)
        self.assertEqual(job.failed_chunks, 1)
        self.assertIsNone(job.output_file)
        self.assertEqual(list(self.outputs.iterdir()), [])

    def test_soffice_only_installation_can_export_pdf(self):
        source = self.uploads / "document.docx"
        pdf = self.outputs / "document.pdf"
        def convert(command, **kwargs):
            self.assertEqual(command[0], "/tools/soffice")
            pdf.write_bytes(b"%PDF-test")
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        with patch.object(server.shutil, "which", side_effect=lambda name: "/tools/soffice" if name == "soffice" else None), \
                patch.object(server.subprocess, "run", side_effect=convert):
            self.assertEqual(server.convert_docx_to_pdf_libreoffice(source, self.outputs), pdf)

    def test_failed_pdf_conversion_does_not_return_stale_file(self):
        source = self.uploads / "document.docx"
        (self.outputs / "document.pdf").write_bytes(b"%PDF-stale")
        with patch.object(server.shutil, "which", return_value="/tools/soffice"), \
                patch.object(server.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, stdout="", stderr="conversion failed")):
            with self.assertRaisesRegex(Exception, "conversion failed"):
                server.convert_docx_to_pdf_libreoffice(source, self.outputs)


if __name__ == "__main__":
    unittest.main()
