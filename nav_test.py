"""导航全量冒烟测试：加载真实 index.html，stub 掉 pywebview 后端，逐个点导航。
用法: python nav_test.py [标签]
"""
import http.server
import json
import os
import re
import sys
import threading

from playwright.sync_api import sync_playwright

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.environ.get("LXULTRA_UI_ROOT", HERE))
OUTPUT = os.path.join(HERE, "shots_nav")
TAG = sys.argv[1] if len(sys.argv) > 1 else "run"

STUB = r"""
window.pywebview = { api: new Proxy({}, {
  get: function (t, k) {
    return function () {
      if (k === 'metrics') return Promise.resolve({
        cpu:{load:0,frequency:null,peakFrequency:null,eCoreFrequency:null,temperature:null,power:null},
        memory:{percent:0,used:0,total:0},
        gpu:{available:false,load:0,clock:0,memoryClock:0,usedMemory:null,totalMemory:null,power:0,name:'',reason:''},
        network:{down:0,up:0,totalDown:0,totalUp:0,adapters:[]},
        temperatures:{sensorStage:'ready',cpu:{},gpu:{}},disks:[]
      });
      if (k === 'optimization_catalog') return Promise.resolve({ok:true,computer:[]});
      if (k === 'optimization_status') return Promise.resolve({ok:true,items:{}});
      if (k === 'processes' || k === 'storage_drives' || k === 'fm_rules') return Promise.resolve([]);
      if (k === 'fm_auto_settings') return Promise.resolve({enabled:false,dwellSeconds:30,maxMB:100,folders:[]});
      return Promise.resolve({
        ok: true, items: [], drives: [], disks: [], partitions: [],
        themes: [], message: '', running: false, available: false
      });
    };
  }
}) };
"""


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=ROOT, **kw)

    def log_message(self, *a):
        pass

    def guess_type(self, path):
        if path.endswith(".js"):
            return "application/javascript; charset=utf-8"
        if path.endswith(".css"):
            return "text/css; charset=utf-8"
        return super().guess_type(path)


class Server(http.server.ThreadingHTTPServer):
    request_queue_size = 64
    daemon_threads = True


def main():
    html = open(os.path.join(ROOT, "index.html"), encoding="utf-8").read()
    asset_refs = re.findall(r'(?:src|href)="([^"]+\.(?:js|css|png|svg|ico))"', html)
    missing_assets = [asset for asset in asset_refs if not os.path.isfile(os.path.join(ROOT, asset))]
    if missing_assets:
        print("缺少页面资源:", missing_assets)
        return 1

    srv = Server(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    results = []
    errors = []
    missing_resources = []
    failed_requests = []
    scroll_failures = []
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page(viewport={"width": 1600, "height": 1100})
        page.add_init_script(STUB)
        page.on("console", lambda m: errors.append("console: %s @ %s" % (m.text, m.location))
                if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append("pageerror: %s" % e))
        page.on("requestfailed", lambda request: failed_requests.append(
            "%s (%s)" % (request.url, request.failure)))
        local_origin = "http://127.0.0.1:%d/" % port
        page.on("response", lambda response: missing_resources.append(response.url)
                if response.status >= 400 and response.url.startswith(local_origin) else None)
        page.goto("http://127.0.0.1:%d/index.html" % port, wait_until="load")
        page.wait_for_timeout(800)
        # The browser harness has no native startup handshake; remove the
        # loading cover so navigation tests can exercise the actual controls.
        page.evaluate("document.getElementById('startup-splash')?.remove()")

        nav = page.eval_on_selector_all("#nav button", "els => els.map(e => e.dataset.pane)")
        print("导航项 (%d):" % len(nav), nav)
        panes_outside_main = page.eval_on_selector_all(
            ".pane", "els => els.filter(e => !e.closest('main')).map(e => e.id)")
        if panes_outside_main:
            scroll_failures.append("panes escaped the app's main area: %s" % panes_outside_main)
        print("主界面外的页面:", panes_outside_main)

        for pid in nav:
            page.click('#nav button[data-pane="%s"]' % pid)
            page.wait_for_timeout(450)
            active = page.eval_on_selector_all(
                ".pane", "els => els.filter(e => e.classList.contains('active')).map(e => e.id)")
            title = page.text_content("#page-title")
            ok = bool(active) and title and "undefined" not in (title or "")
            results.append((pid, ok, active, title))

        def panel_is_visible(selector):
            return page.locator(selector).evaluate("""el => {
              const box=el.getBoundingClientRect();
              const style=getComputedStyle(el);
              return !el.hidden && style.display!=='none' && box.width>0 && box.height>20
                && el.textContent.trim().length>20;
            }""")

        page.click('#nav button[data-pane="filemanager"]')
        file_tabs = page.eval_on_selector_all('[data-fm-tab]',
            'els => els.map(el => el.dataset.fmTab)')
        for tab in file_tabs:
            page.click('[data-fm-tab="%s"]' % tab)
            if not panel_is_visible('#fm-section-%s' % tab):
                scroll_failures.append('File Manager tab is blank: %s' % tab)
        page.click('[data-fm-tab="organize"]')
        file_subtabs = page.eval_on_selector_all('[data-fm-subtab]',
            'els => els.map(el => el.dataset.fmSubtab)')
        for tab in file_subtabs:
            page.click('[data-fm-subtab="%s"]' % tab)
            if not panel_is_visible('#fm-section-%s' % tab):
                scroll_failures.append('File Manager subtab is blank: %s' % tab)
        print('收纳清理内部页面:', len(file_tabs), '个主标签,', len(file_subtabs), '个子标签')

        page.click('#nav button[data-pane="optimization"]')
        optimization_tabs = page.eval_on_selector_all('#optimization [data-optimization-view]',
            'els => els.map(el => el.dataset.optimizationView)')
        for tab in optimization_tabs:
            page.click('#optimization [data-optimization-view="%s"]' % tab)
            if not panel_is_visible('#optimization [data-optimization-panel="%s"]' % tab):
                scroll_failures.append('Optimization tab is blank: %s' % tab)
        print('电脑优化内部页面:', optimization_tabs)

        page.click('#nav button[data-pane="programs"]')
        program_tabs = page.eval_on_selector_all('#program-management-tabs [data-program-view]',
            'els => els.map(el => el.dataset.programView)')
        for tab in program_tabs:
            page.click('#program-management-tabs [data-program-view="%s"]' % tab)
            if not panel_is_visible('#%s.pane' % tab):
                scroll_failures.append('Program Management tab is blank: %s' % tab)
        print('程序管理内部页面:', program_tabs)

        page.click('#nav button[data-pane="settings"]')
        settings_tabs = page.eval_on_selector_all('#settings [data-settings-section]',
            'els => els.map(el => el.dataset.settingsSection)')
        for tab in settings_tabs:
            page.click('#settings [data-settings-section="%s"]' % tab)
            if not panel_is_visible('#settings [data-settings-panel="%s"]' % tab):
                scroll_failures.append('Settings tab is blank: %s' % tab)
        print('更新设置内部页面:', settings_tabs)

        page.click('#nav button[data-pane="theme"]')
        if not panel_is_visible('#theme .panel'):
            scroll_failures.append('Theme page is blank')

        page.click('#nav button[data-pane="filemanager"]')
        page.wait_for_timeout(80)
        page.evaluate("""() => {
          const app=document.querySelector('.app');
          const nav=document.getElementById('nav');
          const main=document.querySelector('main');
          app.style.height='720px'; app.style.gridTemplateRows='minmax(0,1fr)';
          nav.style.flex='0 0 150px'; nav.style.height='150px';
          main.style.height='720px'; main.style.maxHeight='720px'; main.style.minHeight='0'; main.style.overflow='auto';
          const filler=document.createElement('div'); filler.style.height='1800px';
          main.append(filler);
          nav.scrollTop=nav.scrollHeight; main.scrollTop=main.scrollHeight;
        }""")
        before = page.evaluate("""() => ({
          nav:document.getElementById('nav').scrollTop,
          main:document.querySelector('main').scrollTop,
          mainHeight:document.querySelector('main').clientHeight,
          mainScrollHeight:document.querySelector('main').scrollHeight
        })""")
        if before["nav"] <= 0 or before["main"] <= 0:
            scroll_failures.append("scroll regression setup failed: %r" % before)
        page.click('#nav button[data-pane="filemanager"]', force=True)
        page.wait_for_timeout(80)
        after = page.evaluate("""() => ({
          nav:document.getElementById('nav').scrollTop,
          main:document.querySelector('main').scrollTop
        })""")
        if after["nav"] != 0 or after["main"] != 0:
            scroll_failures.append("opening File Manager did not reset both scroll areas: %r" % after)
        print("切换到收纳清理时的滚动位置:", before, "=>", after)

        page.evaluate("""() => {
          const main=document.querySelector('main');
          const nav=document.getElementById('nav');
          const filler=document.createElement('div'); filler.style.height='1600px';
          main.append(filler);
          main.scrollTop=main.scrollHeight; nav.scrollTop=nav.scrollHeight;
        }""")
        for pid in nav:
            page.click('#nav button[data-pane="%s"]' % pid, force=True)
            page.wait_for_timeout(120)
            positions = page.evaluate("""() => ({
              nav:document.getElementById('nav').scrollTop,
              main:document.querySelector('main').scrollTop,
              window:document.scrollingElement?.scrollTop || 0
            })""")
            if any(value != 0 for value in positions.values()):
                scroll_failures.append("opening %s left a scroll offset: %r" % (pid, positions))
        print("全部主导航归顶检查:", "通过" if not scroll_failures else scroll_failures)

        page.set_viewport_size({"width": 900, "height": 900})
        page.click('#nav button[data-pane="optimization"]')
        page.click('#optimization [data-optimization-view="cdrive"]')
        page.wait_for_timeout(100)
        cdrive_preview = page.evaluate("""() => {
          const layout=document.querySelector('#cdrive-opt .cdrive-layout');
          const rows=[...document.querySelectorAll('#cdrive-category-list .cdrive-category')];
          const software=document.querySelector('[data-cdrive-category-toggle="software"]');
          const ai=document.querySelector('[data-cdrive-category-toggle="ai"]');
          return {
            visible:!document.getElementById('cdrive-opt').hidden,
            categoryCount:rows.length,
            softwareChecked:software?.checked ?? null,
            aiChecked:ai?.checked ?? null,
            columns:getComputedStyle(layout).gridTemplateColumns.trim().split(/\\s+/).length,
            textAlign:getComputedStyle(document.querySelector('.cdrive-category-copy')).textAlign,
            overflows:rows.some(row=>row.getBoundingClientRect().right>layout.getBoundingClientRect().right+1)
          };
        }""")
        if cdrive_preview != {"visible": True, "categoryCount": 6, "softwareChecked": False,
                              "aiChecked": False,
                              "columns": 1, "textAlign": "left", "overflows": False}:
            scroll_failures.append("C-drive category layout is not aligned: %r" % cdrive_preview)
        print("窄窗口 C 盘类别布局:", cdrive_preview)

        page.evaluate("document.querySelector('main').scrollTop=450")
        page.click('#optimization [data-optimization-view="drivers"]')
        page.wait_for_timeout(120)
        driver_preview = page.evaluate("""() => {
          const panel=document.getElementById('driver-updates');
          const card=panel?.querySelector('.driver-panel');
          return {
            activeButton:document.querySelector('[data-optimization-view="drivers"]')?.getAttribute('aria-pressed'),
            insideOptimization:!!panel?.closest('#optimization'),
            hidden:panel?.hidden ?? true,
            display:panel ? getComputedStyle(panel).display : 'missing',
            cardHeight:card?.getBoundingClientRect().height ?? 0,
            hasScanButton:!!panel?.querySelector('#driver-scan'),
            mainScrollTop:document.querySelector('main')?.scrollTop ?? -1
          };
        }""")
        if not (driver_preview["activeButton"] == "true" and driver_preview["insideOptimization"]
                and not driver_preview["hidden"]
                and driver_preview["display"] != "none" and driver_preview["cardHeight"] > 0
                and driver_preview["hasScanButton"] and driver_preview["mainScrollTop"] == 0):
            scroll_failures.append("Driver Updates pane did not become visible at the top: %r" % driver_preview)
        print("驱动更新面板:", driver_preview)
        page.screenshot(path=os.path.join(OUTPUT, "cdrive_ui_layout_%s.png" % TAG))

        # 桌面组件里的按钮(收起视图)
        page.evaluate("() => { const b=document.getElementById('collapse'); if(b) b.click(); }")
        page.wait_for_timeout(600)
        widget_btns = page.eval_on_selector_all(
            ".widget-actions button", "els => els.map(e => e.id + ':' + e.textContent.trim())")
        print("桌面组件按钮:", widget_btns)

        page.screenshot(path=os.path.join(OUTPUT, "nav_%s.png" % TAG))
        browser.close()

    if missing_resources:
        scroll_failures.append("HTTP resource errors: %s" % sorted(set(missing_resources)))
    if errors:
        scroll_failures.append("Browser console errors: %s" % errors[:5])
    if failed_requests:
        scroll_failures.append("Browser requests failed: %s" % failed_requests[:5])

    srv.shutdown()
    srv.server_close()
    print("\n--- 逐项结果 ---")
    bad = 0
    for pid, ok, active, title in results:
        print(("  OK  " if ok else "  FAIL") + " %-14s active=%s title=%s" % (pid, active, title))
        if not ok:
            bad += 1
    print("\n错误数: %d" % len(errors))
    print("本地资源 HTTP 错误: %s" % (sorted(set(missing_resources)) or "无"))
    print("失败请求: %s" % (failed_requests or "无"))
    for e in errors[:12]:
        print("   ", e[:220])
    print("\n汇总: %d 页可用 / %d 页异常 / %d 条控制台错误" % (len(results) - bad, bad, len(errors)))
    for failure in scroll_failures:
        print("  滚动修复失败:", failure)

    with open(os.path.join(OUTPUT, "report_%s.json" % TAG), "w", encoding="utf-8") as f:
        json.dump({"nav": nav, "results": results, "errors": errors, "widget": widget_btns},
                  f, ensure_ascii=False, indent=1)
    return 0 if bad == 0 and not scroll_failures else 1


if __name__ == "__main__":
    os.makedirs(OUTPUT, exist_ok=True)
    sys.exit(main())
