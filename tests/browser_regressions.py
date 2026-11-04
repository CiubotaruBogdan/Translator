"""Frontend regressions against real HTML/JS with isolated API fixtures.

Install Playwright separately, then run: python tests/browser_regressions.py
Uses Chromium from CHROMIUM_PATH/system PATH or Playwright's installed browser.
No real application data, network engine, or browser clipboard is used.
"""
import json
import os
from pathlib import Path
import shutil
import unittest
from urllib.parse import urlsplit, unquote

from playwright.sync_api import sync_playwright

STATIC = Path(__file__).resolve().parents[1] / 'app' / 'static'

FAKE_BROWSER_APIS = """(() => {
  window.copiedTexts = [];
  document.execCommand = command => {
    if (command !== 'copy') return false;
    window.copiedTexts.push(document.querySelector('textarea[readonly]').value);
    return true;
  };
  window.auditSockets = [];
  class AuditSocket {
    static CONNECTING = 0; static OPEN = 1; static CLOSING = 2; static CLOSED = 3;
    constructor() {
      this.readyState = 0; this.messages = []; window.auditSockets.push(this);
      queueMicrotask(() => { if (this.readyState === 0) { this.readyState = 1; this.onopen?.(); } });
    }
    send(message) { this.messages.push(message); }
    close() { this.readyState = 3; queueMicrotask(() => this.onclose?.()); }
  }
  window.WebSocket = AuditSocket;
  window.auditPingTimers = new Set();
  const originalSet = window.setInterval, originalClear = window.clearInterval;
  window.setInterval = (fn, delay, ...args) => {
    const id = originalSet(fn, delay, ...args);
    if (delay === 20000) window.auditPingTimers.add(id);
    return id;
  };
  window.clearInterval = id => { window.auditPingTimers.delete(id); originalClear(id); };
})();"""


def fixtures(path):
    if path == '/api/system-info':
        return {'ollama_url': 'http://127.0.0.1:11434', 'libretranslate_url': 'http://127.0.0.1:5000',
                'installed_languages': ['en', 'ro'], 'platform': 'Linux', 'python': '3.11',
                'cpu_count': 4, 'ram_total_gb': 8, 'server_start': '2026-01-01T00:00:00',
                'uptime_seconds': 1, 'upload_dir': '/tmp/upload', 'output_dir': '/tmp/output',
                'total_jobs': 0, 'active_jobs': 0}
    if path == '/api/models':
        return {'connected': True, 'models': [{'name': 'model:latest', 'size': 1000, 'details': {}}]}
    if path == '/api/jobs':
        return {'jobs': []}
    if path == '/api/libreoffice':
        return {'available': True, 'version': 'fixture'}
    if path == '/api/ocr/status':
        return {'tesseract_available': True, 'libreoffice': {'available': True}}
    if path == '/api/logs':
        return {'logs': [], 'total': 0}
    if path == '/api/files':
        return {'files': []}
    if path == '/api/prompts':
        return {'prompts': [], 'total': 0}
    return {}


class FrontendRegressions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        executable = os.environ.get('CHROMIUM_PATH') or shutil.which('chromium') or shutil.which('google-chrome')
        options = {'headless': True, 'args': ['--no-sandbox']}
        if executable:
            options['executable_path'] = executable
        cls.browser = cls.playwright.chromium.launch(**options)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.context = self.browser.new_context()
        self.context.add_init_script(FAKE_BROWSER_APIS)
        self.page = self.context.new_page()
        self.errors = []
        self.page.on('pageerror', lambda error: self.errors.append(str(error)))
        self.overrides = {}
        self.requests = []
        def route(request):
            path = urlsplit(request.request.url).path
            self.requests.append((request.request.method, path))
            if path in self.overrides:
                request.fulfill(json=self.overrides[path])
            elif path.startswith('/api/'):
                request.fulfill(json=fixtures(path))
            elif path.startswith('/static/'):
                file = STATIC / Path(path).name
                request.fulfill(content_type='application/javascript', body=file.read_text())
            else:
                file = STATIC / (Path(path).name or 'index.html')
                request.fulfill(content_type='text/html', body=file.read_text())
        self.page.route('**/*', route)

    def tearDown(self):
        self.context.close()
        self.assertEqual(self.errors, [])

    def open(self, name='index.html'):
        self.page.goto('http://translator-browser.test/' + name, wait_until='networkidle')

    def exchange(self, text):
        return {'id': 'stable-id', 'engine': 'ollama', 'kind': 'single', 'src': 'en', 'tgt': 'ro',
                'model': 'model', 'job_id': 'job', 'attempts': 1, 'prompt_chars': len(text),
                'response_chars': len(text), 'duration_ms': 1, 'time': '2026-01-01',
                'prompt': text, 'response': text}

    def test_filename_delete_handles_apostrophes_without_executing_code(self):
        name = "report');window.injected=true;('O'Brien.txt"
        self.overrides['/api/files'] = {'files': [{'category': 'upload', 'name': name,
            'size': 3, 'modified': '2026-01-01T00:00:00'}]}
        self.open('logs.html')
        href = self.page.locator('.file-download').get_attribute('href')
        self.assertEqual(unquote(href), '/api/files/upload/' + name)
        self.assertEqual(self.page.locator('.file-download').get_attribute('download'), name)
        self.page.on('dialog', lambda dialog: dialog.accept())
        self.page.locator('#filesPanel button').click()
        self.page.wait_for_function('window.injected !== true')
        deletes = [unquote(path) for method, path in self.requests if method == 'DELETE']
        self.assertEqual(deletes, ['/api/files/upload/' + name])
        self.assertFalse(self.page.evaluate('window.injected === true'))

    def test_history_copy_is_literal_and_works_on_http(self):
        text = "It's good; '-window.injected=true-' <img src=x>"
        self.overrides['/api/prompts'] = {'prompts': [self.exchange(text)], 'total': 1}
        self.open('prompts.html')
        self.assertFalse(self.page.evaluate('!!navigator.clipboard'))
        self.page.locator('.ex-head').click()
        self.page.locator('.copy-btn').first.click()
        self.page.locator('.copy-btn').last.click()
        self.assertEqual(self.page.evaluate('copiedTexts'), [text, text])
        self.assertFalse(self.page.evaluate('window.injected === true'))

    def test_history_refresh_preserves_open_entry(self):
        self.overrides['/api/prompts'] = {'prompts': [self.exchange('Example')], 'total': 1}
        self.open('prompts.html')
        self.page.locator('.ex-head').click()
        self.page.evaluate('loadPrompts()')
        self.assertIn('open', self.page.locator('.ex').get_attribute('class'))
        self.page.locator('.ex-head').press('Enter')
        self.assertNotIn('open', self.page.locator('.ex').get_attribute('class'))

    def test_model_metadata_is_literal_in_settings_dashboard_and_gpu(self):
        payload = '<img src=x onerror="window.metadataExecuted=true">'
        model = {'name': payload, 'size': 1024,
                 'details': {'family': payload, 'parameter_size': payload}}
        self.overrides['/api/models'] = {'connected': True, 'models': [model]}
        self.open()
        self.page.wait_for_function("document.querySelector('#settingsModelsList td') !== null")
        self.assertEqual(self.page.locator('#settingsModelsList').inner_text().count(payload), 3)
        self.assertIn(payload, self.page.locator('#modelSelect').inner_text())
        self.page.evaluate('''data => ws.onmessage({data: JSON.stringify({type: 'update', data})})''', {
            'system': {'gpu_models': [{'model': payload, 'size': 1024, 'vram': 512}]},
            'ollama': {'connected': True, 'models': [model]},
        })
        self.assertEqual(self.page.locator('#dashModelsList').inner_text().count(payload), 2)
        self.assertIn(payload, self.page.locator('#dashGpuModels').inner_text())
        self.assertEqual(self.page.locator('#settingsModelsList img, #dashModelsList img, #dashGpuModels img').count(), 0)
        self.assertFalse(self.page.evaluate('window.metadataExecuted === true'))

    def test_ocr_and_text_copy_fallback_on_http(self):
        self.open()
        self.page.evaluate("""() => {
            document.querySelector('#ocrResultText').textContent = 'OCR text';
            document.querySelector('#ttOutput').textContent = 'Translated text';
            document.querySelector('#ttOutput').classList.remove('placeholder');
            copyOCRText(); copyTTOutput();
        }""")
        self.assertEqual(self.page.evaluate('copiedTexts'), ['OCR text', 'Translated text'])

    def test_language_switch_chooses_valid_fallback_and_empty_submit_is_blocked(self):
        self.open()
        self.page.evaluate("""() => {
            $('sourceLang').value='de'; $('ttSourceLang').value='fr';
            $('engineSelect').value='libretranslate'; onEngineChange();
        }""")
        self.page.wait_for_function("$('sourceLang').options.length === 2")
        for ident in ['sourceLang', 'targetLang', 'ttSourceLang', 'ttTargetLang']:
            self.assertIn(self.page.locator('#' + ident).input_value(), ['en', 'ro'])
        self.page.evaluate("""() => {
            $('ttSourceLang').value=''; $('ttInput').value='A sentence'; translateText();
            $('sourceLang').value=''; selectedFiles=[new File(['test'],'test.txt')]; startTranslation();
        }""")
        self.assertFalse(any(method == 'POST' and path in ['/api/translate', '/api/translate-text'] for method, path in self.requests))

    def test_review_without_suggestions_keeps_export_and_download(self):
        self.open()
        self.page.evaluate("""() => {
            ensureReviewModal(); $('reviewModal').classList.add('open');
            applyReviewData({status:'completed',results:[{index:0,verdict:'ok',source:'a',translation:'b',suggestion:'',issues:[]}],revised_output_file:'existing.docx'});
        }""")
        self.assertTrue(self.page.locator('#rvExportHtmlBtn').is_visible())
        self.assertTrue(self.page.locator('#rvDownloadLink').is_visible())
        self.assertTrue(self.page.locator('#rvApplyBtn').is_hidden())
        self.page.evaluate("applyReviewData({status:'running',results:[]})")
        self.assertTrue(self.page.locator('#rvDownloadLink').is_hidden())

    def test_upload_success_removes_only_submitted_file(self):
        self.open()
        self.page.evaluate("""() => {
            const originalFetch=window.fetch;
            window.fetch=(url,options)=>url==='/api/translate'
              ? new Promise(resolve=>window.finishUpload=()=>resolve(new Response(JSON.stringify({job_id:'test'}),{status:200})))
              : originalFetch(url,options);
            window.EventSource=class {close(){}};
            selectedFiles=[new File(['first'],'first.txt')]; window.upload=startTranslation();
            addFiles([new File(['second'],'second.txt')]);
        }""")
        self.page.evaluate('finishUpload(); upload')
        self.assertEqual(self.page.evaluate('selectedFiles.map(f=>f.name)'), ['second.txt'])
        self.assertFalse(self.page.locator('#translateBtn').is_disabled())

    def test_upload_failure_keeps_retry_file_and_new_arrival(self):
        self.open()
        self.page.evaluate("""() => {
            const originalFetch=window.fetch;
            window.fetch=(url,options)=>url==='/api/translate'
              ? new Promise(resolve=>window.finishUpload=()=>resolve(new Response(JSON.stringify({detail:'rejected'}),{status:400})))
              : originalFetch(url,options);
            selectedFiles=[new File(['first'],'first.txt')]; window.upload=startTranslation();
            addFiles([new File(['second'],'second.txt')]);
        }""")
        self.page.evaluate('finishUpload(); upload')
        self.assertEqual(self.page.evaluate('selectedFiles.map(f=>f.name)'), ['first.txt', 'second.txt'])
        self.assertFalse(self.page.locator('#translateBtn').is_disabled())

    def test_websocket_reconnect_has_one_ping_timer(self):
        self.open()
        self.assertEqual(self.page.evaluate('auditPingTimers.size'), 1)
        for _ in range(4):
            self.page.evaluate('connectWS()')
        self.assertEqual(self.page.evaluate('auditPingTimers.size'), 1)
        self.page.evaluate('ws.close()')
        self.assertEqual(self.page.evaluate('auditPingTimers.size'), 0)
        self.page.evaluate('connectWS()')
        self.assertEqual(self.page.evaluate('auditPingTimers.size'), 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
