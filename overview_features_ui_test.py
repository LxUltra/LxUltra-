"""Browser checks for the benchmark and driver update workflows."""
import os
import unittest
from pathlib import Path
from playwright.sync_api import sync_playwright

HERE = os.path.dirname(__file__)


class OverviewFeatureUITest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(channel='msedge', headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def test_benchmark_shows_running_motion_and_result_tier(self):
        page = self.browser.new_page()
        page.set_content('''<div id="benchmark-panel"><button id="benchmark-start"></button>
          <button id="benchmark-cancel"></button><p id="benchmark-status"></p>
          <span id="benchmark-elapsed"></span><div id="benchmark-progress"><div class="benchmark-progress-track"><span id="benchmark-progress-bar"></span></div><strong id="benchmark-progress-value"></strong></div>
          <div id="benchmark-activity"><span id="benchmark-activity-text"></span></div>
          <div id="benchmark-results"></div><div id="benchmark-verdict"></div></div>''')
        page.evaluate("""() => { window.pywebview={api:{
          benchmark_start:async()=>({state:'running',message:'Windows 正在评测'}),
          benchmark_status:async()=>({state:'complete',message:'完成',scores:{WinSPRLevel:8.2,CPUScore:9.1,MemoryScore:8.5,DiskScore:8.2}}),
          benchmark_cancel:async()=>({state:'cancelled'})}} }""")
        page.add_script_tag(path=os.path.join(HERE, 'hardware_overview_ui.js'))
        page.evaluate('window.LxHardwareUI.initialize()')
        page.click('#benchmark-start')
        page.wait_for_function("document.querySelector('#benchmark-panel').classList.contains('is-running')")
        self.assertFalse(page.locator('#benchmark-progress').evaluate('(el) => el.hidden'))
        self.assertRegex(page.locator('#benchmark-progress-value').inner_text(), r'\d+%')
        self.assertIn('估算', page.locator('#benchmark-activity-text').inner_text())
        self.assertGreater(int(page.locator('#benchmark-progress').get_attribute('aria-valuenow')), 0)
        page.wait_for_function("document.querySelector('#benchmark-verdict').textContent.includes('性能')", timeout=5000)
        # The displayed verdict uses the arithmetic mean of available Windows subscores,
        # while also showing the strongest component; it must not use WinSPRLevel's
        # lowest-score composite as the user's overall performance label.
        self.assertIn('8.6', page.locator('#benchmark-verdict').inner_text())
        self.assertIn('9.1', page.locator('#benchmark-verdict').inner_text())
        page.close()

    def test_driver_scan_lists_updates_without_preselecting_install(self):
        page = self.browser.new_page()
        page.set_content('''<div id="driver-updates"><button id="driver-scan">检测驱动</button>
          <button id="driver-install">安装</button><p id="driver-status"></p>
          <div id="driver-list"></div></div>''')
        page.evaluate("""() => { window.pywebview={api:{
          driver_updates_scan:async()=>({ok:true,state:'scanning',updates:[]}),
          driver_updates_status:async()=>({ok:true,state:'ready',message:'找到 1 项',updates:[{key:'A:1',title:'显卡驱动',size:1024}]}),
          driver_updates_install:async()=>({ok:true,state:'installing'})}} }""")
        page.add_script_tag(path=os.path.join(HERE, 'driver_updates_ui.js'))
        page.evaluate('window.LxDriverUpdates.initialize()')
        page.click('#driver-scan')
        page.wait_for_function("document.querySelector('#driver-list').textContent.includes('显卡驱动')", timeout=5000)
        page.wait_for_function("!document.querySelector('#driver-scan').disabled", timeout=5000)
        self.assertEqual(page.locator('#driver-list input:checked').count(), 0)
        self.assertTrue(page.locator('#driver-install').is_disabled())
        page.evaluate('window.confirm = () => true; window.installedKeys = null; window.pywebview.api.driver_updates_install = async keys => {window.installedKeys=keys; return {ok:true,state:"installing",updates:[]}}')
        page.locator('#driver-list input').check()
        self.assertFalse(page.locator('#driver-install').is_disabled())
        page.click('#driver-install')
        page.wait_for_function('window.installedKeys !== null')
        self.assertEqual(page.evaluate('window.installedKeys'), ['A:1'])
        page.close()

    def test_driver_page_starts_first_scan_automatically(self):
        page = self.browser.new_page()
        page.set_content('''<div id="driver-updates"><button id="driver-scan"></button><button id="driver-install"></button>
          <p id="driver-status"></p><div id="driver-list"></div></div>''')
        page.evaluate("""() => {window.scanCalls=0;window.pywebview={api:{
          driver_updates_scan:async()=>{window.scanCalls++;return {ok:true,state:'scanning',updates:[]}},
          driver_updates_status:async()=>({ok:true,state:'idle',updates:[]})}}}""")
        page.add_script_tag(path=os.path.join(HERE, 'driver_updates_ui.js'))
        page.evaluate('window.LxDriverUpdates.initialize();window.LxDriverUpdates.enter()')
        page.wait_for_function('window.scanCalls === 1')
        page.evaluate('window.LxDriverUpdates.enter()')
        self.assertEqual(page.evaluate('window.scanCalls'), 1)
        page.close()

    def test_driver_scanning_has_visible_motion(self):
        page = self.browser.new_page()
        page.set_content('''<div id="driver-updates"><div class="driver-panel"><button id="driver-scan"></button>
          <button id="driver-install"></button><div class="driver-status-row"><span class="driver-pulse"></span>
          <p id="driver-status"></p></div><div id="driver-list"></div></div></div>''')
        page.add_style_tag(path=os.path.join(HERE, 'hardware_tools_ui.css'))
        page.add_style_tag(path=os.path.join(HERE, 'driver_updates_ui.css'))
        page.evaluate("""() => {window.pywebview={api:{driver_updates_scan:async()=>({ok:true,state:'scanning',updates:[]})}}}""")
        page.add_script_tag(path=os.path.join(HERE, 'driver_updates_ui.js'))
        page.evaluate('window.LxDriverUpdates.initialize()')
        page.click('#driver-scan')
        page.wait_for_function("document.querySelector('#driver-updates').classList.contains('is-running')")
        style = page.locator('.driver-pulse').evaluate('(el) => ({animation:getComputedStyle(el).animationName,width:el.getBoundingClientRect().width})')
        self.assertEqual(style['animation'], 'driver-spin')
        self.assertGreaterEqual(style['width'], 16)
        line = page.locator('.driver-status-row').evaluate("(el) => getComputedStyle(el,'::after').animationName")
        self.assertEqual(line, 'driver-scan-line')
        page.close()

    def test_driver_download_shows_reported_percent(self):
        page = self.browser.new_page()
        page.set_content('''<div id="driver-updates"><button id="driver-scan"></button><button id="driver-install"></button>
          <div class="driver-status-row"><span class="driver-pulse"></span><p id="driver-status"></p>
          <span id="driver-elapsed"></span></div><div id="driver-progress" hidden><div id="driver-progress-bar"></div>
          <span id="driver-progress-text"></span></div><div id="driver-list"></div></div>''')
        page.evaluate("""() => {window.pywebview={api:{driver_updates_status:async()=>({ok:true,state:'installing',
          phase:'downloading',percent:37,message:'正在下载',updates:[]})}}}""")
        page.add_script_tag(path=os.path.join(HERE, 'driver_updates_ui.js'))
        page.evaluate('window.LxDriverUpdates.initialize(); window.LxDriverUpdates.refresh()')
        page.wait_for_function("document.querySelector('#driver-progress-text').textContent.includes('37%')")
        self.assertFalse(page.locator('#driver-progress').evaluate('(el) => el.hidden'))
        self.assertEqual(page.locator('#driver-progress').get_attribute('aria-valuenow'), '37')
        page.close()

    def test_processes_and_startup_are_grouped_under_program_management(self):
        html = (Path(HERE) / 'index.html').read_text(encoding='utf-8')
        nav = html.split('id="nav"', 1)[1].split('</nav>', 1)[0]
        self.assertEqual(nav.count('data-pane="programs"'), 1)
        self.assertNotIn('data-pane="processes"', nav)
        self.assertNotIn('data-pane="startup"', nav)
        self.assertIn('id="program-management-tabs"', html)
        self.assertIn('data-program-view="startup"', html)
        self.assertIn('data-program-view="processes"', html)


if __name__ == '__main__':
    unittest.main()
