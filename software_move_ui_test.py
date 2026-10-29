"""Browser checks for the separate software-management and software-move views."""
from pathlib import Path
import unittest

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent


class SoftwareMoveUITest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(channel="msedge", headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def test_move_requires_target_and_preview_confirmation(self):
        page = self.browser.new_page()
        page.set_content("""<!doctype html><meta charset="utf-8"><div id="fm-notice" role="status"></div>
          <section id="fm-section-software" class="fm-hidden fm-layout">
            <div id="old-software-list" class="fm-panel">Installed list</div><aside id="old-software-help" class="fm-panel">Help</aside>
          </section>""")
        page.evaluate("""() => {
          window.moveCalls=[];
          window.pywebview={api:{
            fm_choose:async()=>['D:\\\\Software Migration'],
            fm_software_list:async()=>({ok:true,items:[
              {id:'move-me',name:'Example App',publisher:'Example Inc',version:'1.0',sizeBytes:2048,
               installLocation:'C:\\\\Program Files\\\\Example App',canMove:true,moveReason:''},
              {id:'protected',name:'Windows Component',sizeBytes:0,installLocation:'C:\\\\Windows',canMove:false,
               moveReason:'系统目录和 Microsoft Store 应用受到保护'}
            ]}),
            fm_software_icons:async()=>({ok:true,icons:{}}),
            fm_software_move:async(id,target)=>{window.moveCalls.push([id,target]);return {ok:true,destination:target+'\\\\Example App',bytes:2048}}
          }};
          window.fmtBytes=value=>`${value} B`;
        }""")
        page.add_script_tag(content=(ROOT / "software_manager_ui.js").read_text(encoding="utf-8"))
        page.click('[data-software-view="move"]')
        page.wait_for_selector('[data-software-move="move-me"]')
        self.assertTrue(page.locator('[data-software-move="protected"]').is_disabled())
        self.assertEqual(page.evaluate("window.moveCalls"), [])
        page.click("#fm-software-move-target")
        self.assertIn("D:", page.locator("#fm-software-move-target-path").inner_text())
        page.click('[data-software-move="move-me"]')
        self.assertFalse(page.locator("#fm-software-move-dialog").evaluate("el=>el.hidden"))
        self.assertEqual(page.evaluate("window.moveCalls"), [])
        self.assertIn("C:\\Program Files", page.locator("#fm-software-move-source").inner_text())
        page.click("#fm-software-move-confirm")
        page.wait_for_function("window.moveCalls.length===1")
        self.assertEqual(page.evaluate("window.moveCalls"), [["move-me", "D:\\Software Migration"]])
        page.close()

    def test_production_index_loads_the_move_interface(self):
        html = (ROOT / "index.html").read_text(encoding="utf-8")
        self.assertIn('<script src="file_ui.js"></script><script src="software_manager_ui.js"></script>', html)
        self.assertIn("data-software-view=\"move\"", (ROOT / "software_manager_ui.js").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
