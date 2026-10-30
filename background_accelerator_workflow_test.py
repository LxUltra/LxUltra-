"""Exercise actual accelerator UI with isolated process data; never touch real processes."""
import unittest
import os
from pathlib import Path
from playwright.sync_api import sync_playwright, expect
from nav_test import STUB

ROOT = Path(__file__).parent
UI_ROOT = Path(os.environ.get('LXULTRA_UI_ROOT', ROOT))


class BackgroundAcceleratorWorkflowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(channel='msedge', headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.page = self.browser.new_page(viewport={'width': 600, 'height': 650})
        self.page.add_init_script(STUB)
        self.page.add_init_script("""(() => {
          const base=window.pywebview.api;
          const row={pid:101,started:123.5,name:'test-background.exe',exe:'D:/Test/test-background.exe',
            memory:209715200,cpu:0,cpuReady:true,protected:false};
          window.testCalls={scans:0,execute:0};
          window.pywebview.api=new Proxy({}, {get:(_,key)=>{
            if(key==='processes')return async()=>{window.testCalls.scans++;return [row,{pid:102}]};
            if(key==='process_background_windows')return async()=>({ok:true,visiblePids:[]});
            if(key==='close_preview')return async(mode,selected)=>({ok:true,token:'isolated-test',
              targets:selected.filter(x=>x.pid===101&&x.started===123.5).map(()=>row)});
            if(key==='close_execute')return async()=>{window.testCalls.execute++;return {ok:true,results:[{ok:true,pid:101,name:row.name}]}};
            return base[key];
          }});
        })()""")
        self.page.goto((UI_ROOT / 'index.html').as_uri())
        self.page.evaluate("document.getElementById('startup-splash')?.remove()")

    def tearDown(self):
        self.page.close()

    def open_widget(self):
        self.page.evaluate("document.body.classList.add('compact')")
        self.page.locator('#widget-accelerate').click()
        expect(self.page.locator('#accelerator-process-summary')).to_contain_text('已勾选 1 个')

    def test_widget_scan_and_cancel_confirmation(self):
        self.open_widget()
        expect(self.page.locator('#accelerator-process-status')).to_have_attribute('data-state', 'complete')
        expect(self.page.locator('#accelerator-process-run')).to_be_enabled()
        self.page.locator('#accelerator-process-run').click()
        expect(self.page.locator('#confirm-dialog')).to_be_visible()
        expect(self.page.locator('#confirm-message')).to_contain_text('test-background.exe')
        self.page.locator('#confirm-cancel').click()
        expect(self.page.locator('#accelerator-process-status')).to_contain_text('已取消')
        self.assertEqual(self.page.evaluate('window.testCalls.execute'), 0)

    def test_heading_quick_action_is_clickable_and_reaches_confirmation(self):
        self.open_widget()
        self.page.locator('#widget-action-content').get_by_role('button', name='一键加速', exact=True).click(timeout=1500)
        expect(self.page.locator('#confirm-dialog')).to_be_visible()
        self.assertEqual(self.page.evaluate('window.testCalls.execute'), 0)
        self.page.locator('#confirm-cancel').click()

    def test_unchecked_items_disable_acceleration(self):
        self.open_widget()
        self.page.locator('#accelerator-process-clear').click()
        expect(self.page.locator('#accelerator-process-run')).to_be_disabled()
        expect(self.page.locator('#accelerator-quick-start')).to_be_disabled()
        self.page.locator('#accelerator-process-select-all').click()
        expect(self.page.locator('#accelerator-process-run')).to_be_enabled()

    def test_main_panel_quick_action_scans_before_confirmation(self):
        self.page.evaluate("document.body.classList.remove('compact');showPane('accelerate')")
        self.page.locator('#accelerator-quick-start').click()
        expect(self.page.locator('#confirm-dialog')).to_be_visible()
        self.page.locator('#confirm-cancel').click()
        self.assertEqual(self.page.evaluate('window.testCalls.execute'), 0)

    def test_program_management_uses_same_preview_and_cancel(self):
        self.page.evaluate("document.body.classList.remove('compact');showPane('programs')")
        self.page.locator('[data-program-view="processes"]').click()
        self.page.locator('#process-background-scan').click()
        expect(self.page.locator('#process-background-summary')).to_contain_text('已勾选 1 个')
        self.page.locator('#process-background-run').click()
        expect(self.page.locator('#confirm-dialog')).to_be_visible()
        self.page.locator('#confirm-cancel').click()
        expect(self.page.locator('#process-background-status')).to_contain_text('已取消')
        self.assertEqual(self.page.evaluate('window.testCalls.execute'), 0)

    def test_confirm_executes_only_after_acceptance(self):
        self.open_widget()
        self.page.locator('#accelerator-quick-start').click()
        expect(self.page.locator('#confirm-dialog')).to_be_visible()
        self.assertEqual(self.page.evaluate('window.testCalls.execute'), 0)
        self.page.locator('#confirm-yes').click()
        expect(self.page.locator('#accelerator-process-status')).to_contain_text('关闭 1 个')
        self.assertEqual(self.page.evaluate('window.testCalls.execute'), 1)
        expect(self.page.locator('#accelerator-process-run')).to_be_disabled()

    def test_window_scan_failure_can_be_retried(self):
        # Replace only the OS boundary; exercise the real error and retry rendering.
        self.page.evaluate("""() => {
          const api=window.pywebview.api;
          let first=true;
          window.pywebview.api=new Proxy({}, {get:(_,key)=>key==='process_background_windows'
            ?async()=>{if(first){first=false;return {ok:false,error:'窗口识别失败'}}return {ok:true,visiblePids:[]}}
            :api[key]});
          document.body.classList.add('compact');
        }""")
        self.page.locator('#widget-accelerate').click()
        expect(self.page.locator('#accelerator-process-status')).to_contain_text('窗口识别失败')
        expect(self.page.locator('#accelerator-process-scan')).to_be_enabled()
        expect(self.page.locator('#accelerator-process-run')).to_be_disabled()
        self.page.locator('#accelerator-process-scan').click()
        expect(self.page.locator('#accelerator-process-summary')).to_contain_text('已勾选 1 个')
        expect(self.page.locator('#accelerator-process-run')).to_be_enabled()

    def test_repeated_widget_open_scan_and_return_preserve_layout(self):
        self.page.evaluate("document.body.classList.add('compact')")
        for _ in range(20):
            self.page.locator('#widget-accelerate').click()
            expect(self.page.locator('#accelerator-process-summary')).to_contain_text('已勾选 1 个')
            self.assertEqual(self.page.locator('#accelerator-process-list input:checked').count(), 1)
            self.page.locator('#widget-panel-close').click()
            expect(self.page.locator('#widget-action-overlay')).to_be_hidden()
            self.assertTrue(self.page.locator('main #accelerate').count() == 1)
        self.assertEqual(self.page.evaluate('window.testCalls.execute'), 0)

    def test_reopening_widget_action_resets_scroll_to_visible_primary_action(self):
        self.open_widget()
        content=self.page.locator('#widget-action-content')
        content.evaluate('el=>el.scrollTop=300')
        self.assertGreater(content.evaluate('el=>el.scrollTop'), 0)
        self.page.locator('#widget-panel-close').click()
        self.page.locator('#widget-accelerate').click()
        expect(self.page.locator('#accelerator-process-summary')).to_contain_text('已勾选 1 个')
        self.assertEqual(content.evaluate('el=>el.scrollTop'), 0)


if __name__ == '__main__':
    unittest.main()
