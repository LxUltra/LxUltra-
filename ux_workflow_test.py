"""Focused user-flow regressions for the navigation and safe defaults."""
from pathlib import Path
import re
import unittest

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parent
HTML = (ROOT / "index.html").read_text(encoding="utf-8")


class UXWorkflowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(channel="msedge", headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def test_startup_metadata_is_not_offered_as_a_program(self):
        page = self.browser.new_page()
        self.assertIn("function visibleStartupItems(items)", HTML)
        helper = HTML.split("function visibleStartupItems(items)", 1)[1].split(
            "function startupDisplayName(item)", 1)[0]
        page.add_script_tag(content="function visibleStartupItems(items)" + helper)
        names = page.evaluate("""() => visibleStartupItems([
          {name:'desktop.ini',command:'C:\\\\Users\\\\A\\\\Startup\\\\desktop.ini'},
          {name:'DESKTOP.INI',command:'C:\\\\ProgramData\\\\Startup\\\\DESKTOP.INI'},
          {name:'MyApp',command:'C:\\\\Apps\\\\MyApp.exe'}
        ]).map(item=>item.name)""")
        self.assertEqual(names, ["MyApp"])
        page.close()

    def test_startup_display_name_uses_actual_app_not_command_wrapper(self):
        page = self.browser.new_page()
        helper = HTML.split("function startupDisplayName(item)", 1)[1].split(
            "async function loadStartupItems()", 1
        )[0]
        page.add_script_tag(content="function startupDisplayName(item)" + helper)
        name = page.evaluate("""() => startupDisplayName({name:'com.vendor.app',
          command:'C:\\\\Windows\\\\System32\\\\cmd.exe /c C:\\\\Apps\\\\Writer\\\\Writer.exe'})""")
        self.assertEqual(name, "Writer")
        page.close()

    def test_quick_acceleration_stops_at_review_after_scan(self):
        page = self.browser.new_page()
        page.set_content("""<button id='accelerator-quick-start'>扫描可加速项</button>
          <button id='accelerator-process-scan'>扫描后台占用</button>
          <p id='accelerator-process-status'></p><div id='accelerator-process-actions' hidden></div>
          <span id='accelerator-process-summary'></span><div id='accelerator-process-list'></div>
          <button id='accelerator-process-select-all'></button><button id='accelerator-process-clear'></button>
          <button id='accelerator-process-run'></button>""")
        page.evaluate("""() => {window.previewCalls=0;window.pywebview={api:{
          processes:async()=>[{pid:111,started:1000,name:'TestApp',exe:'C:\\\\Apps\\\\TestApp.exe',
                            protected:false,memory:200000000,cpu:1}],
          process_background_windows:async()=>({ok:true,visiblePids:[]})}};
          window.LxProcessCleanup={preview:async()=>{window.previewCalls++;return {ok:true,closed:1}}}}""")
        page.add_script_tag(path=str(ROOT / "background_accelerator_ui.js"))
        page.click("#accelerator-quick-start")
        page.wait_for_timeout(500)
        self.assertEqual(page.evaluate("window.previewCalls"), 0)
        self.assertEqual(page.locator("#accelerator-process-list input:checked").count(), 0)
        self.assertTrue(page.locator("#accelerator-process-run").is_disabled())
        page.close()

    def test_acceleration_locks_controls_during_confirmation(self):
        page = self.browser.new_page()
        page.set_content("""<button id='accelerator-quick-start'></button>
          <button id='accelerator-process-scan'></button><p id='accelerator-process-status'></p>
          <div id='accelerator-process-actions'></div><span id='accelerator-process-summary'></span>
          <div id='accelerator-process-list'></div><button id='accelerator-process-select-all'></button>
          <button id='accelerator-process-clear'></button><button id='accelerator-process-run'></button>""")
        page.evaluate("""() => {window.pywebview={api:{processes:async()=>[
          {pid:111,started:1000,name:'TestApp',exe:'C:\\\\Apps\\\\TestApp.exe',
           protected:false,memory:200000000,cpu:1}],
          process_background_windows:async()=>({ok:true,visiblePids:[]})}};
          window.LxProcessCleanup={preview:()=>new Promise(()=>{})}}""")
        page.add_script_tag(path=str(ROOT / "background_accelerator_ui.js"))
        page.click("#accelerator-process-scan")
        page.locator("#accelerator-process-list input").check()
        page.click("#accelerator-process-run")
        self.assertTrue(page.locator("#accelerator-process-scan").is_disabled())
        self.assertTrue(page.locator("#accelerator-process-select-all").is_disabled())
        self.assertTrue(page.locator("#accelerator-process-list input").is_disabled())
        page.close()

    def test_file_organizer_starts_with_narrow_scope(self):
        page = self.browser.new_page()
        page.set_content("<div id='file-manager-root'></div>")
        page.evaluate("""() => {window.pywebview={api:new Proxy({}, {get:(_,key)=>async()=>
          key==='storage_drives'?['C:','D:']:[]})};window.askConfirmation=async()=>false}""")
        page.add_script_tag(path=str(ROOT / "file_ui.js"))
        page.wait_for_selector("#fm-organize-all")
        self.assertFalse(page.locator("#fm-organize-all").is_checked())
        self.assertEqual(page.locator("[data-organize-drive]:checked").count(), 0)
        page.close()

    def test_cancelled_folder_picker_keeps_current_scope(self):
        page = self.browser.new_page()
        page.set_content("<div id='file-manager-root'></div>")
        page.evaluate("""() => {window.pywebview={api:new Proxy({}, {get:(_,key)=>async()=>
          key==='storage_drives'?['C:','D:']:[]})};window.askConfirmation=async()=>false}""")
        page.add_script_tag(path=str(ROOT / "file_ui.js"))
        page.wait_for_selector("[data-organize-drive]")
        page.locator("[data-organize-drive]").first.check()
        page.click("#fm-add-folder")
        page.wait_for_timeout(150)
        self.assertEqual(page.locator("[data-organize-drive]:checked").count(), 1)
        page.close()

    def test_all_drive_selection_clears_ignored_folder_scope(self):
        page = self.browser.new_page()
        page.set_content("<div id='file-manager-root'></div>")
        page.evaluate("""() => {window.pywebview={api:new Proxy({}, {get:(_,key)=>async()=>
          key==='storage_drives'?['C:','D:']:key==='fm_choose'?['C:\\\\Work']:[]})};
          window.askConfirmation=async()=>false}""")
        page.add_script_tag(path=str(ROOT / "file_ui.js"))
        page.wait_for_selector("[data-organize-drive]")
        page.click("#fm-add-folder")
        self.assertIn("Work", page.locator("#fm-folders").inner_text())
        page.locator("#fm-organize-all").check()
        self.assertNotIn("Work", page.locator("#fm-folders").inner_text())
        page.close()

    def test_cancelled_file_pickers_do_not_raise_page_errors(self):
        page = self.browser.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.set_content("<div id='file-manager-root'></div>")
        page.evaluate("""() => {window.pywebview={api:new Proxy({}, {get:(_,key)=>async()=>
          key==='storage_drives'?['C:']:null})};window.askConfirmation=async()=>false}""")
        page.add_script_tag(path=str(ROOT / "file_ui.js"))
        for selector in ("#fm-add-files", "#fm-migrate-pick-target", "#fm-clean-folder", "#fm-auto-folders"):
            page.locator(selector).evaluate("el => el.click()")
        page.wait_for_timeout(100)
        self.assertEqual(errors, [])
        page.close()

    def test_disk_meter_can_be_changed_without_auto_rotation(self):
        self.assertIn('id="overview-disk-next"', HTML)
        self.assertIn('id="widget-disk-next"', HTML)
        self.assertNotIn("setInterval(rotateDiskMeter,6000)", HTML)

    def test_widget_action_says_it_scans_before_acceleration(self):
        page = self.browser.new_page()
        page.set_content(HTML)
        self.assertEqual(page.locator("#widget-accelerate").inner_text(), "扫描加速项")
        page.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
