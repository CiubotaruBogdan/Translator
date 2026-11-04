"""Content-integrity, request-bound and lifecycle regression tests.

HTTP model responses are controlled; document assembly and lifecycle code are real.
Run with python -m unittest discover -s tests -v.
"""
import asyncio
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch
import sys

import docx
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import server


class ReachableSession:
    status = 200
    def __init__(self, response=None):
        self.response = response
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        pass
    def get(self, *args, **kwargs):
        return self
    def post(self, *args, **kwargs):
        return self
    async def json(self):
        return {"response": self.response} if self.response is not None else []


class PipelineLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.uploads, self.outputs = self.root / 'uploads', self.root / 'outputs'
        self.uploads.mkdir(); self.outputs.mkdir()
        for name, value in [('UPLOAD_DIR', self.uploads), ('OUTPUT_DIR', self.outputs),
                            ('jobs', {}), ('pipeline_tasks', {}), ('review_tasks', {}),
                            ('_active_file_paths', {})]:
            patcher = patch.object(server, name, value)
            patcher.start(); self.addCleanup(patcher.stop)
        patcher = patch.dict(os.environ, {'TRANSLATOR_ALLOWED_ENGINE_URLS': 'http://engine'})
        patcher.start(); self.addCleanup(patcher.stop)
        patcher = patch.object(server, 'emit')
        patcher.start(); self.addCleanup(patcher.stop)

    def job(self, filename='source.txt', **kwargs):
        job = server.TranslationJob('audit', filename, 'en', 'ro', 'model', 'http://engine', '-', **kwargs)
        server.jobs[job.job_id] = job
        return job

    async def test_batch_rejects_missing_extra_and_empty_parts(self):
        for response in ['Only first', 'One---SEGMENT_BREAK---Two---SEGMENT_BREAK---Extra',
                         'One---SEGMENT_BREAK---']:
            with self.subTest(response=response), patch.object(server, 'MAX_RETRIES', 1):
                with self.assertRaisesRegex(Exception, 'expected 2 nonempty segments'):
                    await server.translate_batch_ollama(ReachableSession(response), ['First', 'Second'],
                        'en', 'ro', 'model', 'http://engine')

    async def test_invalid_batch_falls_back_without_losing_document_segments(self):
        job = self.job('source.docx', batch_size=3)
        document = docx.Document()
        for text in ['First source', 'Second source', 'Third source']:
            document.add_paragraph(text)
        document.save(self.uploads/'audit_source.docx')
        async def individual(session, text, *args, **kwargs):
            return 'Translated '+text, 1
        with patch.object(server.aiohttp, 'ClientSession', return_value=ReachableSession('Only first')), \
             patch.object(server, 'MAX_RETRIES', 1), \
             patch.object(server, 'translate_chunk', side_effect=individual):
            await server.run_pipeline(job)
        self.assertEqual(job.status, server.JobStatus.COMPLETED)
        self.assertEqual(job.failed_chunks, 0)
        result = docx.Document(self.outputs/('audit_'+job.output_file))
        self.assertEqual([p.text for p in result.paragraphs],
                         ['Translated First source', 'Translated Second source', 'Translated Third source'])

    def test_chunk_limit_is_strict_and_no_source_text_is_dropped(self):
        text = 'Long text ' + 'x'*50000 + ' tail'
        chunks = server.create_chunks([text], 500)
        self.assertTrue(all(len(c['text']) <= 500 for c in chunks))
        self.assertEqual(''.join(c['text'] for c in chunks), text)
        self.assertTrue(all(c['char_count']==len(c['text']) for c in chunks))
        self.assertTrue(all(len(c['text']) <= 12 for c in server.create_chunks(['first', 'second', 'third'], 12)))

    async def test_oversized_formatted_docx_is_bounded_and_reassembled(self):
        job = self.job('source.docx', chunk_size=500, batch_size=3)
        document = docx.Document(); paragraph = document.add_paragraph()
        paragraph.add_run('a'*998)
        paragraph.add_run('b'*900).bold = True
        document.save(self.uploads/'audit_source.docx')
        seen=[]
        async def identity(session, text, *args, **kwargs):
            seen.append(text)
            return text, 1
        with patch.object(server.aiohttp, 'ClientSession', return_value=ReachableSession()), \
             patch.object(server, 'translate_chunk', side_effect=identity):
            await server.run_pipeline(job)
        self.assertEqual(job.status, server.JobStatus.COMPLETED)
        self.assertTrue(all(len(t)<=500 for t in seen))
        joined=''.join(seen)
        self.assertEqual(joined.count('⟦0⟧'), 1)
        self.assertEqual(joined.count('⟦1⟧'), 1)
        self.assertTrue(all(not t.endswith('⟦') and not t.endswith('⟦1') for t in seen))
        result=docx.Document(self.outputs/('audit_'+job.output_file))
        self.assertEqual(result.paragraphs[0].text, 'a'*998+'b'*900)
        self.assertTrue(result.paragraphs[0].runs[-1].bold)

    async def test_batch_payload_obeys_combined_chunk_limit(self):
        job = self.job('source.docx', chunk_size=500, batch_size=3)
        document=docx.Document()
        for letter in 'abc': document.add_paragraph(letter*300)
        document.save(self.uploads/'audit_source.docx')
        seen=[]
        async def batch(session,texts,*args,**kwargs):
            seen.append(texts);return texts,1
        with patch.object(server.aiohttp,'ClientSession',return_value=ReachableSession()), \
             patch.object(server,'translate_batch_ollama',side_effect=batch):
            await server.run_pipeline(job)
        self.assertEqual(job.status,server.JobStatus.COMPLETED)
        self.assertTrue(all(len(server.BATCH_SEPARATOR.join(texts))<=500 for texts in seen))

    async def test_partial_failure_does_not_publish_incomplete_document(self):
        job = self.job('source.docx', engine='libretranslate')
        document=docx.Document();document.add_paragraph('Good');document.add_paragraph('Bad')
        document.save(self.uploads/'audit_source.docx')
        async def translate(session,text,*args,**kwargs):
            if text=='Bad': raise RuntimeError('engine failed')
            return 'Translated',1
        with patch.object(server.aiohttp,'ClientSession',return_value=ReachableSession()), \
             patch.object(server,'translate_chunk_libretranslate',side_effect=translate):
            await server.run_pipeline(job)
        self.assertEqual(job.status,server.JobStatus.FAILED)
        self.assertEqual(job.failed_chunks,1)
        self.assertIsNone(job.output_file)
        self.assertEqual(list(self.outputs.iterdir()),[])

    async def test_cancel_interrupts_inflight_request(self):
        job=self.job(engine='libretranslate');(self.uploads/'audit_source.txt').write_text('Source')
        entered=asyncio.Event();cancelled=asyncio.Event()
        async def blocked(*args,**kwargs):
            entered.set()
            try: await asyncio.Event().wait()
            finally: cancelled.set()
        with patch.object(server.aiohttp,'ClientSession',return_value=ReachableSession()), \
             patch.object(server,'translate_chunk_libretranslate',side_effect=blocked):
            task=server.start_pipeline(job)
            await asyncio.wait_for(entered.wait(),1)
            await asyncio.wait_for(server.api_cancel(job.job_id),1)
        self.assertTrue(cancelled.is_set())
        self.assertTrue(task.done())
        self.assertEqual(job.status,server.JobStatus.CANCELLED)
        self.assertEqual(list(self.outputs.iterdir()),[])

    async def test_delete_waits_for_cancellation_and_cleans_sidecars(self):
        job=self.job(engine='libretranslate');(self.uploads/'audit_source.txt').write_text('Source')
        (self.uploads/'audit_converted.docx').write_bytes(b'sidecar')
        entered=asyncio.Event();exited=asyncio.Event()
        async def blocked(*args,**kwargs):
            entered.set()
            try: await asyncio.Event().wait()
            finally: exited.set()
        with patch.object(server.aiohttp,'ClientSession',return_value=ReachableSession()), \
             patch.object(server,'translate_chunk_libretranslate',side_effect=blocked):
            server.start_pipeline(job);await asyncio.wait_for(entered.wait(),1)
            await asyncio.wait_for(server.api_delete(job.job_id),1)
        self.assertTrue(exited.is_set())
        self.assertNotIn(job.job_id,server.jobs)
        self.assertEqual(list(self.uploads.iterdir()),[])
        self.assertEqual(list(self.outputs.iterdir()),[])

    async def test_deleting_job_cannot_start_new_review_while_cancellation_waits(self):
        job=self.job();job.status=server.JobStatus.COMPLETED
        job.source_segments=['Source'];job.translated_chunks=['Translated']
        cancelling=asyncio.Event();release=asyncio.Event()
        async def delayed_cancel(*args,**kwargs):
            cancelling.set();await release.wait()
        with patch.object(server,'cancel_job_tasks',side_effect=delayed_cancel):
            deletion=asyncio.create_task(server.api_delete(job.job_id))
            await asyncio.wait_for(cancelling.wait(),1)
            try:
                with self.assertRaises(server.HTTPException) as error:
                    await server.api_review(job.job_id,server.ReviewRequest(model='reviewer'))
                self.assertEqual(error.exception.status_code,409)
                self.assertEqual(server.review_tasks,{})
            finally:
                release.set();await deletion
        self.assertNotIn(job.job_id,server.jobs)

    async def test_review_cancellation_interrupts_reviewer(self):
        job=self.job();job.status=server.JobStatus.COMPLETED;job.review_status='running'
        entered=asyncio.Event()
        async def blocked(*args,**kwargs):
            entered.set();await asyncio.Event().wait()
        with patch.object(server,'run_review',side_effect=blocked):
            task=server.start_review(job,'reviewer','http://engine')
            await asyncio.wait_for(entered.wait(),1)
            await asyncio.wait_for(server.api_review_cancel(job.job_id),1)
        self.assertTrue(task.done());self.assertEqual(job.review_status,'cancelled')
        self.assertEqual(job.status,server.JobStatus.COMPLETED)

    async def test_cleanup_preserves_active_and_leased_files(self):
        job=self.job();job.status=server.JobStatus.TRANSLATING
        active=self.uploads/'audit_source.txt';active.write_text('active')
        fresh=self.uploads/'fresh.txt';fresh.write_text('fresh')
        old=self.uploads/'old.txt';old.write_text('expired')
        lease=self.outputs/'ocr_work.docx';lease.write_text('processing')
        stale=time.time()-48*3600
        for path in [active,old,lease]:os.utime(path,(stale,stale))
        with server.file_lease(lease):
            self.assertEqual(server.cleanup_old_files(),1)
            self.assertTrue(active.exists());self.assertTrue(lease.exists());self.assertTrue(fresh.exists())
            result=await server.api_cleanup_files()
            self.assertEqual(result['skipped_active'],2)
        self.assertFalse(fresh.exists());self.assertFalse(old.exists())
        self.assertTrue(active.exists());self.assertTrue(lease.exists())

    def test_nested_file_lease_remains_active_until_last_exit(self):
        path=self.uploads/'work.txt';path.write_text('working')
        with server.file_lease(path):
            with server.file_lease(path):self.assertTrue(server.is_file_active(path))
            self.assertTrue(server.is_file_active(path))
        self.assertFalse(server.is_file_active(path))


if __name__=='__main__':
    unittest.main()
