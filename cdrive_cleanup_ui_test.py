"""Regression coverage for the C-drive cleanup progress and completion flow."""
import os
import re
import subprocess
import unittest
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent


class CDriveCleanupUITest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(channel="msedge", headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def test_progress_script_parses(self):
        result = subprocess.run(
            ["node", "--check", str(ROOT / "performance_tools_ui.js")],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_progress_and_completion_have_visible_motion_styles(self):
        page = self.browser.new_page()
        page.set_content("""<!doctype html><meta charset="utf-8">
          <section class="cdrive-scan-progress is-running"><div id="cdrive-scan-progress-track" class="cdrive-progress-track">
          <span id="cdrive-scan-progress-bar"></span></div></section>
          <div id="cdrive-clean-success" class="cdrive-clean-success is-visible"><div class="cdrive-success-card"></div></div>""")
        page.add_style_tag(path=str(ROOT / "hardware_tools_ui.css"))
        bar_animation = page.locator("#cdrive-scan-progress-bar").evaluate("el => getComputedStyle(el).animationName")
        self.assertEqual(bar_animation, "cdrive-progress-glow")
        dialog_style = page.locator("#cdrive-clean-success").evaluate("el => ({display:getComputedStyle(el).display,zIndex:getComputedStyle(el).zIndex})")
        self.assertEqual(dialog_style["display"], "grid")
        self.assertGreaterEqual(int(dialog_style["zIndex"]), 50)
        page.close()

    def test_ai_protection_list_has_clear_path_and_remove_action_layout(self):
        css = (ROOT / "hardware_tools_ui.css").read_text(encoding="utf-8")
        html = (ROOT / "index.html").read_text(encoding="utf-8")
        self.assertIn(".cdrive-ai-exclusion-row", css)
        self.assertIn(".cdrive-ai-protection", css)
        self.assertIn('id="cdrive-ai-detected-list"', html)
        self.assertIn("本次扫描可选的 AI 目录", html)
        self.assertIn('data-cdrive-category-toggle="ai"', html)
        self.assertIn("自建保护目录", html)
        self.assertIn('class="cdrive-ai-protection" open', html)

    def test_detected_ai_directories_render_separately_from_custom_protected_paths(self):
        page = self.browser.new_page()
        page.set_default_timeout(5000)
        page.set_content("""<!doctype html><meta charset="utf-8">
          <button id="cdrive-scan"></button><button id="cdrive-cancel" hidden disabled></button>
          <p id="cdrive-scan-status"></p><section id="cdrive-scan-progress" hidden aria-busy="false">
            <span id="cdrive-scan-progress-label"></span><strong id="cdrive-scan-percent"></strong>
            <div id="cdrive-scan-progress-track" role="progressbar" aria-valuenow="0"><span id="cdrive-scan-progress-bar"></span></div>
            <p id="cdrive-scan-progress-detail"></p></section>
          <section id="cdrive-scan-results" hidden><span id="cdrive-scan-count"></span>
            <button id="cdrive-select-all"></button><button id="cdrive-clear-selection"></button>
            <button id="cdrive-clean-selected" disabled></button><div id="cdrive-result-list"></div>
            <div id="cdrive-ai-detected-list"></div><div id="cdrive-ai-exclusion-list"></div>
            <button id="cdrive-ai-exclusion-add"></button><span id="cdrive-ai-exclusion-status"></span></section>""")
        page.evaluate("""() => {
          let polls=0;
          window.fmtBytes=value=>`${value} B`;
          window.pywebview={api:{
            storage_drives:async()=>['C:'],storage_scan_start:async()=>({ok:true}),
            storage_scan_status:async()=>++polls===1
              ? {running:true,drives:['C:'],scanned:12000}
              : {running:false,drives:['C:'],scanned:24000,items:[],ai_protected_count:2,ai_protected_bytes:300,
                 ai_protected_directories:[
                   {path:'C:\\\\Users\\\\User\\\\.cache\\\\huggingface',count:1,bytes:100,source:'automatic'},
                   {path:'D:\\\\AI Workspace',count:1,bytes:200,source:'custom'}]},
            storage_ai_exclusions_list:async()=>({ok:true,paths:['D:\\\\AI Workspace']})
          }};
        }""")
        page.add_script_tag(content=(ROOT / "performance_tools_ui.js").read_text(encoding="utf-8"))
        page.evaluate("window.LxPerformanceUI.initialize()")
        page.click("#cdrive-scan")
        page.wait_for_function("document.querySelector('#cdrive-scan-results') && !document.querySelector('#cdrive-scan-results').hidden")
        self.assertIn("huggingface", page.locator("#cdrive-ai-detected-list").inner_text())
        self.assertIn("100 B", page.locator("#cdrive-ai-detected-list").inner_text())
        self.assertIn(r"D:\AI Workspace", page.locator("#cdrive-ai-exclusion-list").inner_text())
        self.assertNotIn(r"D:\AI Workspace", page.locator("#cdrive-ai-detected-list").inner_text())
        self.assertTrue(page.locator("#cdrive-cancel").evaluate("el=>el.hidden"))
        page.close()

    def test_ai_candidates_appear_in_their_checkbox_group_and_require_explicit_confirmation(self):
        page = self.browser.new_page()
        page.set_default_timeout(5000)
        page.set_content("""<!doctype html><meta charset="utf-8">
          <button id="cdrive-scan"></button><button id="cdrive-cancel" hidden disabled></button>
          <p id="cdrive-scan-status"></p><section id="cdrive-scan-progress" hidden aria-busy="false">
            <span id="cdrive-scan-progress-label"></span><strong id="cdrive-scan-percent"></strong>
            <div id="cdrive-scan-progress-track" role="progressbar" aria-valuenow="0"><span id="cdrive-scan-progress-bar"></span></div>
            <p id="cdrive-scan-progress-detail"></p></section>
          <section id="cdrive-scan-results" hidden><span id="cdrive-scan-count"></span>
            <button id="cdrive-select-all"></button><button id="cdrive-clear-selection"></button>
            <button id="cdrive-clean-selected" disabled></button><div id="cdrive-result-list"></div>
            <div id="cdrive-clean-result" hidden></div><div id="cdrive-ai-detected-list"></div>
            <div id="cdrive-ai-exclusion-list"></div><button id="cdrive-ai-exclusion-add"></button>
            <span id="cdrive-ai-exclusion-status"></span></section>
          <div id="cdrive-category-list"><label><input type="checkbox" data-cdrive-category-toggle="ai" checked></label></div>""")
        page.evaluate("""() => {
          let polls=0;
          window.cleanedIds=null; window.allowAiCleanup=null; window.confirmationMessage='';
          window.fmtBytes=value=>`${value} B`;
          window.askConfirmation=async(title,message)=>{window.confirmationMessage=message;return true};
          const ai={id:'ai-cache',drive:'C:',category:'ai',path:'C:/Users/User/.cache/huggingface/hub/cache.bin',size:4096};
          window.pywebview={api:{
            storage_drives:async()=>['C:'], storage_scan_start:async()=>({ok:true}),
            storage_scan_status:async()=>++polls===1
              ? {running:true,drives:['C:'],scanned:2000}
              : {running:false,drives:['C:'],scanned:5000,items:[],ai_detected_items:[ai],
                 ai_detected_directories:[{path:'C:/Users/User/.cache/huggingface',count:1,bytes:4096,source:'automatic'}]},
            storage_ai_exclusions_list:async()=>({ok:true,paths:[]}),
            fm_software_list:async()=>({ok:true,items:[]}),
            storage_cleanup:async(ids,allowAi)=>{window.cleanedIds=ids;window.allowAiCleanup=allowAi;return {ok:true,moved:0,failed:0,bytes:0}}
          }};
        }""")
        page.add_script_tag(content=(ROOT / "performance_tools_ui.js").read_text(encoding="utf-8"))
        page.evaluate("window.LxPerformanceUI.initialize()")
        page.click("#cdrive-scan")
        page.wait_for_function("document.querySelector('#cdrive-scan-results') && !document.querySelector('#cdrive-scan-results').hidden")
        ai_box=page.locator('[data-cdrive-id="ai-cache"]')
        self.assertTrue(ai_box.is_visible())
        self.assertFalse(ai_box.is_checked())
        self.assertEqual(ai_box.evaluate("el=>el.closest('[data-cdrive-group]').dataset.cdriveGroup"), "ai")
        self.assertIn("huggingface", page.locator("#cdrive-ai-detected-list").inner_text())
        ai_box.check()
        page.click("#cdrive-clean-selected")
        page.wait_for_function("window.cleanedIds !== null")
        self.assertEqual(page.evaluate("window.cleanedIds"), ["ai-cache"])
        self.assertTrue(page.evaluate("window.allowAiCleanup"))
        self.assertIn("AI", page.evaluate("window.confirmationMessage"))
        self.assertIn("模型或项目", page.evaluate("window.confirmationMessage"))
        page.close()

    def test_active_external_scan_can_be_cancelled_and_failed_cancel_can_retry(self):
        page = self.browser.new_page()
        page.set_default_timeout(5000)
        page.set_content("""<!doctype html><meta charset="utf-8">
          <button id="cdrive-scan"></button><button id="cdrive-cancel" hidden disabled>取消扫描</button>
          <p id="cdrive-scan-status"></p><section id="cdrive-scan-progress" hidden aria-busy="false">
            <span id="cdrive-scan-progress-label"></span><strong id="cdrive-scan-percent"></strong>
            <div id="cdrive-scan-progress-track" role="progressbar" aria-valuenow="0"><span id="cdrive-scan-progress-bar"></span></div>
            <p id="cdrive-scan-progress-detail"></p></section>
          <section id="cdrive-scan-results" hidden><span id="cdrive-scan-count"></span>
            <button id="cdrive-select-all"></button><button id="cdrive-clear-selection"></button>
            <button id="cdrive-clean-selected" disabled></button><div id="cdrive-result-list"></div></section>""")
        page.evaluate("""() => {
          let polls=0, cancels=0;
          window.pywebview={api:{
            storage_drives:async()=>['C:'],storage_scan_start:async()=>({ok:false,error:'扫描已在运行'}),
            storage_scan_status:async()=>{
              polls++;
              return cancels>1
                ? {running:false,drives:['C:'],scanned:100,cancelled:true,items:[]}
                : {running:true,drives:['C:'],scanned:polls*10};
            },
            storage_scan_cancel:async()=>++cancels===1?{ok:false,error:'取消接口暂时失败'}:{ok:true}
          }};
        }""")
        page.add_script_tag(content=(ROOT / "performance_tools_ui.js").read_text(encoding="utf-8"))
        page.evaluate("window.LxPerformanceUI.initialize()")
        page.click("#cdrive-scan")
        page.wait_for_function("!document.querySelector('#cdrive-cancel').hidden")
        self.assertTrue(page.locator("#cdrive-cancel").is_enabled())
        page.click("#cdrive-cancel")
        page.wait_for_function("document.querySelector('#cdrive-cancel').disabled===false")
        self.assertIn("取消接口暂时失败", page.locator("#cdrive-scan-status").inner_text())
        page.click("#cdrive-cancel")
        page.wait_for_function("document.querySelector('#cdrive-cancel').hidden")
        self.assertIn("扫描已取消", page.locator("#cdrive-scan-status").inner_text())
        page.close()

    def test_real_category_controls_keep_safe_defaults_and_aligned_layout(self):
        html = (ROOT / "index.html").read_text(encoding="utf-8")
        css = (ROOT / "hardware_tools_ui.css").read_text(encoding="utf-8")
        controls = re.findall(r'<input type="checkbox" data-cdrive-category-toggle="([^"]+)"([^>]*)>', html)
        self.assertEqual([name for name, _ in controls], ["system", "software", "social", "ai", "installers", "large"])
        self.assertNotIn('data-cdrive-category="wechat"', html)
        self.assertIn("微信", re.search(r'<label class="cdrive-category" data-cdrive-category="social".*?</label>', html, re.S).group(0))
        self.assertNotIn("checked", dict(controls)["software"])
        self.assertNotIn("checked", dict(controls)["ai"])
        ai_category = re.search(r'<label class="cdrive-category" data-cdrive-category="ai".*?</label>', html, re.S).group(0)
        self.assertIn("类别默认关闭", ai_category)
        for name, attributes in controls:
            self.assertIn("aria-label=", attributes)
            self.assertEqual("checked" in attributes, name not in {"software", "ai"})
        self.assertIn(".cdrive-layout .cdrive-category{display:grid;grid-template-columns:18px minmax(0,1fr)", css)
        self.assertIn("@media(max-width:1040px){.cdrive-layout{grid-template-columns:minmax(0,1fr)}", css)

    def test_category_descriptions_stay_left_aligned_in_narrow_layout(self):
        html = (ROOT / "index.html").read_text(encoding="utf-8")
        css_blocks = re.findall(r"<style[^>]*>(.*?)</style>", html, flags=re.S | re.I)
        panel = re.search(
            r'(<div class="panel"><h3>可识别的项目</h3>.*?</div></div>)<p class="note" id="cdrive-safety-copy">',
            html, flags=re.S,
        )
        self.assertIsNotNone(panel)
        page = self.browser.new_page(viewport={"width": 900, "height": 720})
        page.set_content('<!doctype html><meta charset="utf-8"><section class="cdrive-layout">' + panel.group(1) + '</section>')
        for block in css_blocks:
            page.add_style_tag(content=block)
        page.add_style_tag(path=str(ROOT / "hardware_tools_ui.css"))
        copy = page.locator(".cdrive-category-copy").first
        self.assertEqual(copy.evaluate("el=>getComputedStyle(el).textAlign"), "left")
        layout = page.locator(".cdrive-layout").evaluate("el=>getComputedStyle(el).gridTemplateColumns")
        self.assertEqual(len(layout.split()), 1)
        page.close()

    def test_category_filters_are_checkboxes_and_do_not_select_files(self):
        page = self.browser.new_page()
        page.set_content("""<!doctype html><meta charset="utf-8">
          <button id="cdrive-scan"></button><button id="cdrive-cancel" disabled></button>
          <p id="cdrive-scan-status"></p>
          <section id="cdrive-scan-progress" hidden aria-busy="false">
            <span id="cdrive-scan-progress-label"></span><strong id="cdrive-scan-percent"></strong>
            <div id="cdrive-scan-progress-track" role="progressbar" aria-valuenow="0"><span id="cdrive-scan-progress-bar"></span></div>
            <p id="cdrive-scan-progress-detail"></p>
          </section>
          <section id="cdrive-scan-results" hidden><span id="cdrive-scan-count"></span>
            <button id="cdrive-select-all"></button><button id="cdrive-clear-selection"></button>
            <button id="cdrive-clean-selected" disabled></button><div id="cdrive-result-list"></div></section>
          <div id="cdrive-category-list">
            <label class="cdrive-category" data-cdrive-category="system"><input type="checkbox" data-cdrive-category-toggle="system" checked><span><strong>系统不常用文件</strong><small>只列旧临时文件</small></span></label>
            <label class="cdrive-category" data-cdrive-category="software"><input type="checkbox" data-cdrive-category-toggle="software"><span><strong>软件缓存</strong><small>只识别缓存目录</small></span></label>
            <label class="cdrive-category" data-cdrive-category="social"><input type="checkbox" data-cdrive-category-toggle="social" checked><span><strong>社交软件</strong><small>包括微信缓存</small></span></label>
            <label class="cdrive-category" data-cdrive-category="installers"><input type="checkbox" data-cdrive-category-toggle="installers" checked><span><strong>下载的安装包</strong><small>逐项查看后再选择</small></span></label>
          </div>""")
        page.evaluate("""() => {
          const items = [
            {id:'system',drive:'C:',category:'temporary',path:'C:\\\\Windows\\\\Temp\\\\safe.tmp',size:10},
            {id:'cache',drive:'C:',category:'temporary',path:'C:\\\\Users\\\\User\\\\AppData\\\\Local\\\\Microsoft\\\\Edge\\\\Cache\\\\item.tmp',size:1024},
            {id:'wechat',drive:'C:',category:'temporary',path:'C:\\\\Users\\\\User\\\\Documents\\\\WeChat Files\\\\Cache\\\\item.tmp',size:20},
            {id:'installer',drive:'C:',category:'installers',path:'C:\\\\Users\\\\User\\\\Downloads\\\\setup.exe',size:30}
          ];
          let polls=0;
          window.fmtBytes=value=>`${value} B`;
          window.pywebview={api:{
            storage_drives:async()=>['C:'], storage_scan_start:async()=>({ok:true}),
            storage_scan_status:async()=>++polls===1
              ? {running:true,drives:['C:'],scanned:12000}
              : {running:false,drives:['C:'],scanned:24000,items,cancelled:false}
          }};
        }""")
        page.add_script_tag(content=(ROOT / "performance_tools_ui.js").read_text(encoding="utf-8"))
        page.evaluate("window.LxPerformanceUI.initialize()")
        page.click("#cdrive-scan")
        page.wait_for_function("document.querySelector('#cdrive-scan-results') && !document.querySelector('#cdrive-scan-results').hidden")
        self.assertEqual(page.locator("[data-cdrive-group]").evaluate_all("els=>els.map(el=>el.dataset.cdriveGroup)"), ["system", "social", "installers"])
        self.assertEqual(page.locator('[data-cdrive-id="wechat"]').evaluate("el=>el.closest('[data-cdrive-group]').dataset.cdriveGroup"), "social")
        self.assertIn("社交软件", page.locator("#cdrive-result-list").inner_text())
        self.assertNotIn("微信专清", page.locator("#cdrive-result-list").inner_text())
        self.assertFalse(page.locator('[data-cdrive-id="system"]').is_checked())
        self.assertFalse(page.locator('[data-cdrive-id="installer"]').is_checked())
        self.assertFalse(page.locator('[data-cdrive-category-toggle="software"]').is_checked())

        page.locator('[data-cdrive-category-toggle="software"]').check()
        self.assertEqual(page.locator("[data-cdrive-group]").evaluate_all("els=>els.map(el=>el.dataset.cdriveGroup)"), ["system", "software", "social", "installers"])
        page.locator('[data-cdrive-id="cache"]').check()
        self.assertTrue(page.locator("#cdrive-clean-selected").is_enabled())
        page.locator('[data-cdrive-category-toggle="software"]').uncheck()
        self.assertFalse(page.locator("#cdrive-clean-selected").is_enabled())
        self.assertIn("已勾选 0 项", page.locator("#cdrive-scan-count").inner_text())
        page.close()

    def test_scan_progress_keeps_controls_and_reports_estimate(self):
        page = self.browser.new_page()
        page.set_content("""<!doctype html><meta charset="utf-8"><button id="cdrive-scan"></button><button id="cdrive-cancel" disabled></button>
          <p id="cdrive-scan-status"></p>
          <section id="cdrive-scan-progress" hidden aria-busy="false">
            <div><span id="cdrive-scan-progress-label"></span><strong id="cdrive-scan-percent"></strong></div>
          <div id="cdrive-scan-progress-track" class="cdrive-progress-track" role="progressbar" aria-valuenow="0"><span id="cdrive-scan-progress-bar"></span></div>
            <p id="cdrive-scan-progress-detail"></p>
          </section>
          <section id="cdrive-scan-results" hidden><span id="cdrive-scan-count"></span>
            <button id="cdrive-select-all"></button><button id="cdrive-clear-selection"></button>
            <button id="cdrive-clean-selected" disabled></button><div id="cdrive-result-list"></div>
            <div id="cdrive-clean-result" hidden></div></section>""")
        page.evaluate("""() => {
          let polls = 0;
          window.fmtBytes = value => `${value} B`;
          window.pywebview = {api: {
            storage_drives: async () => ['C:'],
            storage_scan_start: async () => ({ok:true}),
            storage_scan_status: async () => ++polls === 1
              ? {running:true, drives:['C:'], scanned:12000, folder:'C:\\\\Users'}
              : {running:false, drives:['C:'], scanned:24000, items:[], cancelled:false}
          }};
        }""")
        page.add_script_tag(content=(ROOT / "performance_tools_ui.js").read_text(encoding="utf-8"))
        page.evaluate("window.LxPerformanceUI.initialize()")
        page.click("#cdrive-scan")
        page.wait_for_function("document.querySelector('#cdrive-scan-progress-label')?.textContent.includes('正在扫描')")
        self.assertFalse(page.locator("#cdrive-scan-progress").evaluate("el => el.hidden"))
        self.assertTrue(page.locator("#cdrive-scan-progress").evaluate("el => el.classList.contains('is-running')"))
        self.assertFalse(page.locator("#cdrive-cancel").evaluate("el => el.hidden"))
        self.assertTrue(page.locator("#cdrive-cancel").is_enabled())
        self.assertRegex(page.locator("#cdrive-scan-percent").inner_text(), r"\d+%")
        self.assertGreater(int(page.locator("#cdrive-scan-progress-track").get_attribute("aria-valuenow")), 0)
        self.assertIn("已检查", page.locator("#cdrive-scan-progress-detail").inner_text())
        page.close()

    def test_selected_cleanup_shows_recycle_bin_result_and_migration_action(self):
        page = self.browser.new_page()
        page.set_content("""<!doctype html><meta charset="utf-8"><button id="cdrive-scan"></button><button id="cdrive-cancel" disabled></button>
          <p id="cdrive-scan-status"></p>
          <section id="cdrive-scan-progress" hidden aria-busy="false">
            <div><span id="cdrive-scan-progress-label"></span><strong id="cdrive-scan-percent"></strong></div>
            <div id="cdrive-scan-progress-track" role="progressbar" aria-valuenow="0"><span id="cdrive-scan-progress-bar"></span></div>
            <p id="cdrive-scan-progress-detail"></p>
          </section>
          <section id="cdrive-scan-results" hidden><span id="cdrive-scan-count"></span>
            <button id="cdrive-select-all"></button><button id="cdrive-clear-selection"></button>
            <button id="cdrive-clean-selected" disabled></button><div id="cdrive-result-list"></div>
            <div id="cdrive-clean-result" hidden></div></section>
          <div id="cdrive-clean-success" role="dialog" hidden>
            <button id="cdrive-clean-success-close"></button><strong id="cdrive-clean-success-size"></strong>
            <p id="cdrive-clean-success-detail"></p><button id="cdrive-success-continue"></button>
            <button id="cdrive-success-migrate"></button>
          </div>""")
        page.evaluate("""() => {
          const items = [
            {id:'system',drive:'C:',category:'temporary',path:'C:\\\\Windows\\\\Temp\\\\safe.tmp',size:10},
            {id:'cache',drive:'C:',category:'temporary',path:'C:\\\\Users\\\\User\\\\AppData\\\\Local\\\\Microsoft\\\\Edge\\\\Cache\\\\item.tmp',size:1024},
            {id:'wechat',drive:'C:',category:'temporary',path:'C:\\\\Users\\\\User\\\\Documents\\\\WeChat Files\\\\Cache\\\\item.tmp',size:20},
            {id:'installer',drive:'C:',category:'installers',path:'C:\\\\Users\\\\User\\\\Downloads\\\\setup.exe',size:30},
            {id:'large',drive:'C:',category:'large',path:'C:\\\\Users\\\\User\\\\Videos\\\\clip.bin',size:40}
          ];
          let polls = 0;
          window.cleanedIds = null;
          window.lastPane = null;
          window.lastTab = null;
          window.fmtBytes = value => `${value} B`;
          window.askConfirmation = async () => true;
          window.showPane = pane => {window.lastPane=pane};
          window.fileManagerOpenTab = tab => {window.lastTab=tab};
          window.pywebview = {api: {
            storage_drives: async () => ['C:'],
            storage_scan_start: async () => ({ok:true}),
            storage_scan_status: async () => ++polls === 1
              ? {running:true, drives:['C:'], scanned:12000, folder:'C:\\\\Users'}
              : {running:false, drives:['C:'], scanned:24000, items, cancelled:false},
            fm_software_list: async () => ({ok:true,items:[{id:'edge-icon',name:'Microsoft Edge'}]}),
            fm_software_icons: async ids => ({ok:true,icons:ids.includes('edge-icon')?{'edge-icon':'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/S3sAAAAASUVORK5CYII='}:{}}),
            storage_cleanup: async ids => {window.cleanedIds=ids;return {ok:true,moved:1,failed:0,bytes:1024}}
          }};
        }""")
        page.add_script_tag(content=(ROOT / "performance_tools_ui.js").read_text(encoding="utf-8"))
        page.evaluate("window.LxPerformanceUI.initialize()")
        page.click("#cdrive-scan")
        page.wait_for_function("document.querySelector('#cdrive-scan-results') && !document.querySelector('#cdrive-scan-results').hidden")
        self.assertEqual(page.locator("#cdrive-result-list input[type=checkbox]:checked").count(), 0)
        self.assertIn("社交软件", page.locator("#cdrive-result-list").inner_text())
        self.assertEqual(page.locator('[data-cdrive-id="wechat"]').evaluate("el=>el.closest('[data-cdrive-group]').dataset.cdriveGroup"), "social")
        image = page.locator('.cdrive-file-icon-image img')
        image.wait_for(state="attached")
        self.assertIn("data:image/png;base64,iVBORw0KGgo", image.get_attribute('src'))
        page.locator('[data-cdrive-id="cache"]').check()
        page.click("#cdrive-clean-selected")
        page.wait_for_function("!document.querySelector('#cdrive-clean-success').hidden")
        self.assertEqual(page.evaluate("window.cleanedIds"), ["cache"])
        self.assertIn("移入回收站", page.locator("#cdrive-clean-success-detail").inner_text())
        page.click("#cdrive-success-migrate")
        self.assertEqual(page.evaluate("window.lastPane"), "filemanager")
        self.assertEqual(page.evaluate("window.lastTab"), "migrate")
        page.close()

    def test_ai_project_protection_directory_can_be_added_and_removed(self):
        page = self.browser.new_page()
        page.set_content("""<!doctype html><meta charset="utf-8">
          <button id="cdrive-ai-exclusion-add"></button><div id="cdrive-ai-exclusion-list"></div>
          <span id="cdrive-ai-exclusion-status"></span><section id="cdrive-scan-results" hidden></section>
          <p id="cdrive-scan-status"></p><button id="cdrive-scan"></button>
          <button id="cdrive-cancel" disabled></button>""")
        page.evaluate("""() => {
          window.protectedDirectories=[];
          const path='C:\\Users\\LxUltra\\AI Workspace';
          window.pywebview={api:{
            fm_choose:async()=>[path],
            storage_ai_exclusions_list:async()=>({ok:true,paths:window.protectedDirectories}),
            storage_ai_exclusions_add:async value=>{window.protectedDirectories.push(value);return {ok:true,paths:window.protectedDirectories.slice(),message:'已保护'}},
            storage_ai_exclusions_remove:async value=>{window.protectedDirectories=window.protectedDirectories.filter(item=>item!==value);return {ok:true,paths:window.protectedDirectories.slice(),message:'已移除'}}
          }};
        }""")
        page.add_script_tag(content=(ROOT / "performance_tools_ui.js").read_text(encoding="utf-8"))
        page.evaluate("window.LxPerformanceUI.initialize()")
        page.click("#cdrive-ai-exclusion-add")
        page.wait_for_function("window.protectedDirectories.length===1")
        page.wait_for_selector(".cdrive-ai-exclusion-row")
        self.assertIn("AI Workspace", page.locator(".cdrive-ai-exclusion-row").inner_text())
        self.assertTrue(page.locator("#cdrive-scan-results").evaluate("el=>el.hidden"))
        page.locator("button[data-cdrive-ai-exclusion-remove]").click()
        page.wait_for_function("window.protectedDirectories.length===0")
        self.assertIn("尚未添加", page.locator("#cdrive-ai-exclusion-list").inner_text())
        page.close()


if __name__ == "__main__":
    unittest.main()
