# -*- coding: utf-8 -*-
"""分区助手 UI + 后端 端到端验收(Playwright/Edge + 真实 PowerShell 后端)

架构:
  - 本地 HTTP 服务:提供分区助手最小页面(真实 partition_ui.js/css)+ /api/<name> 转发到真实后端
  - 后端 = app_wrapper._install_partition_methods 挂到 Dummy 上(真实 PS 调用)
  - Playwright 驱动 Edge 内核:渲染、点击、开弹窗、排队、压力点击,截图留证
"""
import json
import os
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
INTERNAL = HERE
SHOT = os.path.join(HERE, "shots")
os.makedirs(SHOT, exist_ok=True)

# ---------- 真实后端 ----------
sys.path.insert(0, HERE)
import base64
import marshal

WRAP_SRC = (open(os.path.join(HERE, "migration_engine.py"), encoding="utf-8").read() + "\n" +
            open(os.path.join(HERE, "app_wrapper.py"), encoding="utf-8").read())


def load_backend():
    """加载包装器(用玩具模块占位,只为拿到 _install_partition_methods)"""
    toy = "class API:\n    pass\n"
    src = WRAP_SRC.replace("@@ORIG_B64@@", base64.b64encode(marshal.dumps(compile(toy, "app.py", "exec"))).decode(), 1)
    ns = {"__name__": "ui_test_wrapper"}
    exec(compile(src, "app_wrapper_uitest.py", "exec"), ns)
    return ns["_install_partition_methods"]


INSTALL = load_backend()


class Backend:
    pass


INSTALL(Backend)
BK = Backend()

# ---------- HTTP 服务 ----------
HARNESS = b"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="stylesheet" href="/static/partition_ui.css">
<style>
 :root{color-scheme:light;--bg:#F5F6F8;--side:#ECEEF1;--panel:#FFFFFF;--panel2:#F3F4F6;--line:#D9DCE1;--text:#202226;--muted:#616771;--accent:#8A641F;--good:#2F7654;--amber:#875B12;--bad:#B4444B} body{margin:0;padding:18px;background:var(--bg);color:var(--text);font-family:"Microsoft YaHei UI","Segoe UI",sans-serif}
</style></head><body>
<div id="partition-root"></div>
<script>
(function(){
  const names=['partition_disks','partition_health','partition_job_submit','partition_job_status','partition_admin','partition_vhd_list'];
  const api={};
  names.forEach(function(n){ api[n]=function(){ var args=[].slice.call(arguments);
    return fetch('/api/'+n,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(args)}).then(function(r){return r.json();}); }; });
  window.pywebview={api:api};
  window.__uiErrors=[];
  window.addEventListener('error',function(e){ window.__uiErrors.push(String(e.message)); });
})();
</script>
<script src="/static/partition_ui.js"></script>
<script>window.LxPartition.enter();</script>
</body></html>"""

SERVER_LOG = []


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            # Rapid browser navigation can cancel an in-flight harness response.
            pass

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            return self._send(200, HARNESS, "text/html; charset=utf-8")
        if self.path == "/favicon.ico":
            return self._send(204, b"", "image/x-icon")
        if self.path.startswith("/static/"):
            name = os.path.basename(self.path)
            fp = os.path.join(INTERNAL, name)
            if os.path.exists(fp):
                ctype = "text/css" if name.endswith(".css") else "application/javascript"
                return self._send(200, open(fp, "rb").read(), ctype + "; charset=utf-8")
        return self._send(404, b"nf", "text/plain")

    def do_POST(self):
        if not self.path.startswith("/api/"):
            return self._send(404, b"nf", "text/plain")
        name = self.path[5:]
        ln = int(self.headers.get("Content-Length") or 0)
        try:
            args = json.loads(self.rfile.read(ln) or b"[]")
        except Exception:
            args = []
        t0 = time.time()
        try:
            fn = getattr(BK, name)
            res = fn(*args)
        except Exception as e:
            res = {"ok": False, "error": "backend exc: %r" % (e,)}
        dt = time.time() - t0
        SERVER_LOG.append((name, dt))
        body = json.dumps(res, ensure_ascii=False).encode("utf-8")
        self._send(200, body, "application/json; charset=utf-8")


def serve():
    srv = ThreadingHTTPServer(("127.0.0.1", 8877), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv


# ---------- 测试 ----------
def main():
    srv = serve()
    from playwright.sync_api import sync_playwright

    results = []
    def ok(name, cond, detail=""):
        results.append((name, bool(cond), detail))
        print(("  PASS " if cond else "  FAIL ") + name + ("  | " + str(detail) if detail and not cond else ""))

    with sync_playwright() as pw:
        browser = None
        for ch in ("msedge", "chrome"):
            try:
                browser = pw.chromium.launch(channel=ch, headless=True)
                print("browser channel =", ch)
                break
            except Exception as e:
                print("launch %s failed: %s" % (ch, e))
        if not browser:
            browser = pw.chromium.launch(headless=True)
            print("browser = bundled chromium")

        page = browser.new_page(viewport={"width": 1280, "height": 800})
        console_errors = []
        bad_urls = []
        page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: console_errors.append("pageerror: %s\n%s" % (e, getattr(e, "stack", ""))))
        page.on("response", lambda r: bad_urls.append("%s %s" % (r.status, r.url)) if r.status >= 400 else None)

        t0 = time.time()
        page.goto("http://127.0.0.1:8877/", wait_until="domcontentloaded")
        page.wait_for_selector(".pt-wrap", timeout=15000)
        page.wait_for_selector("table.pt-table tbody tr", timeout=60000)
        load_ms = (time.time() - t0) * 1000
        ok("页面加载并渲染分区表", True, "%.0f ms" % load_ms)

        rows = page.query_selector_all("table.pt-table tbody tr")
        disk_rows = page.query_selector_all("tr.pt-disk-row")
        part_rows = page.query_selector_all("tr.pt-part-row")
        segs = page.query_selector_all(".pt-map-segment")
        print("     表格行 %d(磁盘 %d / 分区 %d),可视化块 %d" % (len(rows), len(disk_rows), len(part_rows), len(segs)))
        ok("磁盘行数与后端一致(3 块)", len(disk_rows) == 3, len(disk_rows))
        ok("分区行 > 0", len(part_rows) > 0, len(part_rows))
        ok("磁盘可视化块 > 0", len(segs) > 0, len(segs))

        # 表格内容抽查
        txt = page.inner_text("table.pt-table")
        for kw in ("硬盘 0", "硬盘 1", "GPT"):
            ok("表格含 %s" % kw, kw in txt)
        side = page.inner_text("#pt-side-host")
        ok("侧栏显示系统盘标记", "系统盘" in page.inner_text(".pt-wrap") or "系统" in side)

        # Reference layout contract (expected to fail before the redesign).
        ok("参考布局:独立工具条", page.locator(".pt-ribbon").count() == 1)
        ok("参考布局:表格与磁盘图分区", page.locator(".pt-table-pane").count() == 1 and page.locator(".pt-map-pane").count() == 1)
        ok("参考布局:右侧操作面板", page.locator(".pt-side-pane #pt-side-host").count() == 1)
        ok("参考布局:磁盘按行显示", page.locator(".pt-map-row").count() == 3)
        ok("参考布局:撤销重做按钮", page.locator('[data-act="undo"]').count() == 1 and page.locator('[data-act="redo"]').count() == 1)
        ok("宿主主题强调色保留", page.evaluate("() => getComputedStyle(document.documentElement).getPropertyValue('--accent').trim()") == "#8A641F")

        map_pick = page.evaluate("""() => {
            const seg=document.querySelector('.pt-map-segment[data-map-segment="1"]');
            if(!seg) return null;
            seg.click();
            return {disk:seg.getAttribute('data-disk'), part:seg.getAttribute('data-part')};
        }""")
        if map_pick:
            page.wait_for_timeout(150)
            ok("磁盘图选择与表格同步", page.evaluate("""p => {
                const row=document.querySelector('tr.pt-part-row.sel');
                return !!row && row.getAttribute('data-disk')===p.disk && row.getAttribute('data-part')===p.part;
            }""", map_pick))
        else:
            ok("磁盘图可选中数据分区", False)

        # Clone dialogs are queue-only here. No destructive clone is submitted to physical disks.
        clone_source = page.evaluate("""() => {
            const diskRows=[...document.querySelectorAll('tr.pt-disk-row')];
            const sys=diskRows.find(tr=>tr.textContent.includes('系统盘'))?.getAttribute('data-disk');
            const row=[...document.querySelectorAll('tr.pt-part-row')].find(tr=>tr.getAttribute('data-disk')!==String(sys) && /[A-Z]:/.test(tr.cells[0].textContent));
            if(!row) return null; row.click(); return row.getAttribute('data-disk');
        }""")
        page.wait_for_timeout(150)
        clone_btn=page.locator('#pt-side-host [data-act="clone-data"]')
        if clone_source and clone_btn.count() and clone_btn.is_enabled():
            clone_btn.click(); page.wait_for_timeout(120)
            ok("克隆分区锁定当前源并提示选择目标", page.locator('.pt-clone-prompt').count()==1 and not page.is_visible('#pt-mask.open'))
            same = page.locator('.pt-map-segment.sel')
            if same.count(): same.first.click()
            ok("源分区不能当目标且源保持锁定", page.locator('.pt-clone-prompt').count()==1 and not page.is_visible('#pt-mask.open'))
            for target in page.locator('.pt-map-segment[data-map-segment="1"]').all():
                target.click(); page.wait_for_timeout(50)
                if page.is_visible('#pt-mask.open'): break
            opened=page.is_visible('#pt-mask.open')
            ok("点击有效目标后显示源和目标确认", opened and '源：' in page.inner_text('#pt-modal-body') and '目标：' in page.inner_text('#pt-modal-body'))
            if opened:
                page.click('[data-act="modal-ok"]'); page.wait_for_timeout(120)
                ok("克隆分区经确认进入待执行队列", page.locator('#pt-pending-host .pt-opchip').count()==1)
                page.evaluate("() => document.querySelector('[data-act=\"clear-queue\"]')?.click()")
            else:
                page.locator('[data-act="cancel-clone"]').click()
                ok("克隆分区经确认进入待执行队列", False)
            page.wait_for_timeout(80)
        else:
            ok("克隆分区锁定当前源并提示选择目标", False)

        # Queue-only edit: do not submit any disk modification.
        page.evaluate("""() => {
            const rows=[...document.querySelectorAll('tr.pt-part-row')];
            const diskRows=[...document.querySelectorAll('tr.pt-disk-row')];
            const sys=diskRows.find(tr=>tr.textContent.includes('系统盘'))?.getAttribute('data-disk');
            const row=rows.find(tr=>tr.getAttribute('data-disk')!==String(sys) && /[A-Z]:/.test(tr.cells[0].textContent));
            if(row) row.click();
        }""")
        page.wait_for_timeout(150)
        label_btn=page.locator('[data-act="p-label"]')
        if label_btn.count() and label_btn.is_enabled():
            label_btn.click()
            page.wait_for_timeout(150)
            if page.is_visible("#pt-mask.open"):
                page.fill('[data-field="label"]', "UNDO_TEST")
                page.click('[data-act="modal-ok"]')
                page.wait_for_timeout(150)
                queued_before=page.locator("#pt-pending-host .pt-opchip").count()
                undo_available=page.locator('[data-act="undo"]').count()==1
                redo_available=page.locator('[data-act="redo"]').count()==1
                page.evaluate("() => document.querySelector('[data-act=\"undo\"]')?.click()")
                page.wait_for_timeout(100)
                queued_undo=page.locator("#pt-pending-host .pt-opchip").count()
                page.evaluate("() => document.querySelector('[data-act=\"redo\"]')?.click()")
                page.wait_for_timeout(100)
                queued_redo=page.locator("#pt-pending-host .pt-opchip").count()
                ok("撤销可移除最近排队操作", undo_available and queued_before == 1 and queued_undo == 0)
                ok("重做可恢复已撤销操作", redo_available and queued_undo == 0 and queued_redo == 1)
                page.evaluate("() => document.querySelector('[data-act=\"clear-queue\"]')?.click()")
            else:
                ok("撤销重做队列夹具可打开", False)
        else:
            ok("撤销重做队列夹具可选卷标", False)

        page.screenshot(path=os.path.join(SHOT, "01_partition_list.png"), full_page=True)

        # 选中一个分区
        first_part = page.evaluate("""() => {
            const diskRows=[...document.querySelectorAll('tr.pt-disk-row')];
            const sys=diskRows.find(tr=>tr.textContent.includes('系统盘'))?.getAttribute('data-disk');
            const row=[...document.querySelectorAll('tr.pt-part-row')].find(tr=>tr.getAttribute('data-disk')!==String(sys) && /[A-Z]:/.test(tr.cells[0].textContent));
            if(row){row.click();return true;} return false;
        }""")
        if first_part:
            page.wait_for_timeout(300)
            side2 = page.inner_text("#pt-side-host")
            ok("点击分区后侧栏出现分区操作", "格式化" in side2 or "更改盘符" in side2)

        # 打开格式化弹窗并取消
        page.evaluate("""() => {
            const diskRows=[...document.querySelectorAll('tr.pt-disk-row')];
            const sys=diskRows.find(tr=>tr.textContent.includes('系统盘'))?.getAttribute('data-disk');
            const el=[...document.querySelectorAll('tr.pt-part-row')].find(tr=>tr.getAttribute('data-disk')!==String(sys) && /[A-Z]:/.test(tr.cells[0].textContent));
            if(el) el.click();
        }""")
        page.wait_for_timeout(250)
        btn = page.query_selector('[data-act="p-format"]:not([disabled])')
        if btn:
            btn.click()
            page.wait_for_timeout(300)
            ok("格式化弹窗打开", page.is_visible("#pt-mask.open"))
            page.screenshot(path=os.path.join(SHOT, "02_format_dialog.png"))
            page.click('[data-act="modal-cancel"]')
            page.wait_for_timeout(200)
            ok("弹窗可取消关闭", not page.is_visible("#pt-mask.open"))
        else:
            ok("格式化按钮可用(可点)", False, "未找到可用按钮")

        # ===== UI 提交链路用模拟任务；真实写入型验证由临时 VHD 测试负责 =====
        page.evaluate("""() => {
            const rows=[...document.querySelectorAll('tr.pt-disk-row')];
            const sys=rows.find(tr=>tr.textContent.indexOf('系统盘')>=0);
            window.__sysDisk = sys ? sys.getAttribute('data-disk') : '-1';
        }""")
        picked = page.evaluate("""() => {
            const sys = window.__sysDisk;
            const rows=[...document.querySelectorAll('tr.pt-part-row')];
            const row = rows.find(tr => tr.getAttribute('data-disk') !== String(sys) && /[A-Z]:/.test(tr.cells[0].textContent));
            if (!row) return null;
            row.click();
            return {disk: row.getAttribute('data-disk'), part: row.getAttribute('data-part'), text: row.cells[0].textContent};
        }""")
        ok("找到可测速的分区(非系统盘)", bool(picked), picked)
        if picked:
            page.wait_for_timeout(300)
            ok("测速按钮可用", page.evaluate("() => { const b=document.querySelector('[data-act=\"op-bench\"]'); return b && !b.disabled; }"))
            page.locator('[data-act="menu"][data-menu="test"]').click()
            page.locator('.pt-menu.open [data-act="op-bench"]').click()
            page.locator('#pt-modal-body [data-field="pick"]').select_option(
                'part:%s:%s' % (picked["disk"], picked["part"]))
            page.locator('#pt-modal-body [data-act="modal-ok"]').click()
            page.wait_for_timeout(300)
            ok("测速进入待执行队列", page.is_visible("#pt-pending-host .pt-pending"))
            page.screenshot(path=os.path.join(SHOT, "06_queue_bench.png"))
            job_polls = [0]
            def mock_job(route):
                if route.request.url.endswith("partition_job_submit"):
                    body=json.dumps({"ok":True,"job_id":"ui-test-job"})
                else:
                    job_polls[0] += 1
                    if job_polls[0] <= 2:
                        body=json.dumps({"ok":True,"state":"running","current":1,"total":1,"progress":"正在执行测速","results":[]},ensure_ascii=False)
                    else:
                        body=json.dumps({"ok":True,"state":"done","current":1,"total":1,"progress":"UI 测试完成","results":[{"ok":True,"type":"benchmark","op":{"type":"benchmark","letter":picked["text"].split(":")[0]},"message":"模拟写入 123 MB/s · 读取 456 MB/s"}]},ensure_ascii=False)
                route.fulfill(status=200,content_type="application/json; charset=utf-8",body=body)
            page.route("**/api/partition_job_submit", mock_job)
            page.route("**/api/partition_job_status", mock_job)
            page.evaluate("() => document.querySelector('#pt-pending-host [data-act=\"submit\"]')?.click()")
            page.wait_for_timeout(400)
            ok("提交确认弹窗出现", page.is_visible("#pt-mask.open"))
            page.screenshot(path=os.path.join(SHOT, "07_submit_confirm.png"))
            page.click('[data-act="modal-ok"]')
            page.wait_for_selector('#pt-progress-host .pt-progress-activity', timeout=10000)
            ok("单项任务执行中不会误报 100%", page.locator('#pt-progress-host [role="progressbar"]').get_attribute('aria-valuenow') == '0')
            # 等待执行完成
            done = False
            t_exec = time.time()
            for _ in range(120):
                page.wait_for_timeout(1000)
                body = page.inner_text("#pt-progress-host") if page.query_selector("#pt-progress-host") else ""
                if "MB/s" in body or "失败" in body or "完成" in body:
                    done = True
                    break
                if time.time() - t_exec > 120:
                    break
            exec_s = time.time() - t_exec
            body = page.inner_text("#pt-progress-host") or ""
            print("     模拟执行耗时 %.1fs,结果区: %s" % (exec_s, body.replace("\n", " | ")[:200]))
            ok("提交后任务执行并返回结果", done, body[:200])
            ok("结果区出现测速数据(/MB/s)", "MB/s" in body, body[:200])
            page.screenshot(path=os.path.join(SHOT, "08_exec_result.png"), full_page=True)

        # 队列移除测试(改卷标,不提交)
        page.evaluate("""() => {
            const rows=[...document.querySelectorAll('tr.pt-part-row')];
            const row=rows.find(tr=>/[A-Z]:/.test(tr.cells[0].textContent));
            if(row) row.click();
        }""")
        page.wait_for_timeout(300)
        page.evaluate("() => { const b=document.querySelector('[data-act=\"p-label\"]'); if(b&&!b.disabled) b.click(); }")
        page.wait_for_timeout(300)
        if page.is_visible("#pt-mask.open"):
            page.fill('[data-field="label"]', "TEST_LABEL")
            page.click('[data-act="modal-ok"]')
            page.wait_for_timeout(300)
            ok("改卷标操作进入队列", page.is_visible("#pt-pending-host .pt-pending"))
            page.evaluate("() => document.querySelector('#pt-pending-host [data-act=\"clear-queue\"]')?.click()")
            page.wait_for_timeout(250)
            ok("队列可清空", not page.is_visible("#pt-pending-host .pt-pending"))

        # 菜单展开
        page.click('[data-act="menu"][data-menu="test"]')
        page.wait_for_timeout(200)
        ok("工具栏菜单可展开", page.evaluate("() => !!document.querySelector('.pt-menu.open')"))
        ok("工具栏菜单首项实际可见且可点击", page.evaluate("""() => {
            const item=document.querySelector('.pt-menu.open .pt-menu-pop button');
            if(!item) return false;
            const r=item.getBoundingClientRect();
            const x=r.left+r.width/2, y=r.top+r.height/2;
            const hit=document.elementFromPoint(x,y);
            return r.width>0 && r.height>0 && !!hit && (hit===item || item.contains(hit));
        }"""))
        page.screenshot(path=os.path.join(SHOT, "04_menu_open.png"))
        page.click('[data-act="menu"][data-menu="test"]')

        ok("磁盘图位于分区表上方", page.evaluate("() => document.querySelector('.pt-map-pane').getBoundingClientRect().top < document.querySelector('.pt-table-pane').getBoundingClientRect().top"))
        ok("工具栏不显示管理员文字", page.locator('.pt-ribbon .pt-admin-state').count() == 0)
        for width in (1280, 800):
            page.set_viewport_size({"width": width, "height": 800})
            for menu in ("clone", "convert", "recover", "erase", "test", "tools", "vhd"):
                page.locator('[data-act="menu"][data-menu="%s"]' % menu).click()
                hit = page.evaluate("""() => {
                  const items=[...document.querySelectorAll('.pt-menu.open .pt-menu-pop button')];
                  return items.length>0 && items.every(item => {
                    const r=item.getBoundingClientRect(), x=r.left+r.width/2, y=r.top+r.height/2;
                    const el=document.elementFromPoint(x,y);
                    return x>=0 && x<innerWidth && y>=0 && y<innerHeight && !!el && (el===item || item.contains(el));
                  });
                }""")
                ok("%d 宽度 %s 菜单所有项目可点击" % (width, menu), hit)
                page.locator('[data-act="menu"][data-menu="%s"]' % menu).click()
        page.set_viewport_size({"width": 1280, "height": 800})

        page.locator('[data-act="menu"][data-menu="test"]').click()
        page.locator('.pt-menu.open [data-act="op-bench"]').click()
        ok("测试菜单先弹出分区选择", page.locator('#pt-modal-body [data-field="pick"]').count() == 1)
        page.locator('#pt-modal-body [data-field="pick"]').select_option(index=1)
        page.locator('#pt-modal-body [data-act="modal-ok"]').click()
        page.wait_for_timeout(150)
        queued_bench = page.locator('#pt-pending-host .pt-opchip').count() == 1
        ok("从测试菜单真实点击将测速加入队列", queued_bench)
        if queued_bench:
            page.locator('#pt-pending-host [data-act="clear-queue"]').click()
        deadline = time.time() + 30
        while time.time() < deadline and sum(1 for n, _ in SERVER_LOG if n == 'partition_health') < len(disk_rows):
            page.wait_for_timeout(300)
        ok("自动查询每块磁盘健康", sum(1 for n, _ in SERVER_LOG if n == 'partition_health') >= len(disk_rows))
        ok("磁盘详情显示健康度或不可用原因", '健康度：' in page.inner_text('#pt-side-host'))

        # 健康(SMART)按需查询
        page.locator('[data-act="menu"][data-menu="test"]').click()
        page.locator('.pt-menu.open [data-act="op-health"]').click()
        ok("健康检查先弹出磁盘选择", page.locator('#pt-modal-body [data-field="pick"]').count() == 1)
        page.locator('#pt-modal-body [data-field="pick"]').select_option(index=1)
        page.locator('#pt-modal-body [data-act="modal-ok"]').click()
        page.wait_for_selector('#pt-modal-body .pt-health-result', timeout=30000)
        ok("健康查询已触发", any(n == "partition_health" for n, _ in SERVER_LOG))
        ok("健康结果在弹窗直接可见", '健康状态' in page.inner_text('#pt-modal-body .pt-health-result'))
        page.locator('#pt-modal-body [data-act="modal-cancel"]').click()

        # 压力测试:连续刷新 20 次
        t = time.time()
        for i in range(20):
            page.evaluate("() => document.querySelector('[data-act=\"op-refresh\"]')?.click()")
            page.wait_for_timeout(60)
        page.wait_for_timeout(3000)
        stress_s = time.time() - t
        disk_calls = [d for n, d in SERVER_LOG if n == "partition_disks"]
        ok("压力:20 次刷新无 JS 致命错误", len(console_errors) == 0, console_errors[:3])
        print("     压力耗时 %.1fs,partition_disks 调用 %d 次,平均 %.2fs" % (
            stress_s, len(disk_calls), (sum(disk_calls) / len(disk_calls)) if disk_calls else 0))
        ok("刷新节流生效(不会每次都打后端)", len(disk_calls) <= 6, len(disk_calls))

        # 压力测试:反复切换选中 60 次
        page.wait_for_selector('tr.pt-part-row', timeout=60000)
        page.evaluate("""() => {
            const rows=[...document.querySelectorAll('tr.pt-part-row')];
            for(let i=0;i<60;i++){ rows[i%rows.length].click(); }
        }""")
        page.wait_for_timeout(500)
        ok("压力:60 次快速选中无异常", len(console_errors) == 0, console_errors[:3])

        # 重新加载页面(冷启动)测第二次加载速度
        t2 = time.time()
        page.reload(wait_until="domcontentloaded")
        page.wait_for_selector("table.pt-table tbody tr", timeout=60000)
        reload_ms = (time.time() - t2) * 1000
        print("     冷启动重载 %.0f ms" % reload_ms)
        ok("冷启动重载 < 15s", reload_ms < 15000, "%.0fms" % reload_ms)
        page.screenshot(path=os.path.join(SHOT, "05_final.png"), full_page=True)

        ok("全程无 console 错误", len(console_errors) == 0, console_errors[:5])
        mobile = browser.new_page(viewport={"width": 800, "height": 900})
        mobile.goto("http://127.0.0.1:8877/", wait_until="domcontentloaded")
        mobile.wait_for_selector(".pt-map-row", timeout=60000)
        mobile_layout = mobile.evaluate("""() => ({
            columns:getComputedStyle(document.querySelector('.pt-layout')).gridTemplateColumns.trim().split(/\\s+/).length,
            overflow:document.documentElement.scrollWidth-document.documentElement.clientWidth,
            sideTop:document.querySelector('.pt-side-pane').getBoundingClientRect().top,
            mapRows:document.querySelectorAll('.pt-map-row').length
        })""")
        ok("窄窗口改为单列布局", mobile_layout["columns"] == 1, mobile_layout)
        ok("窄窗口无页面级横向溢出", mobile_layout["overflow"] <= 2, mobile_layout)
        mobile.screenshot(path=os.path.join(SHOT, "09_mobile_layout.png"), full_page=True)
        mobile.close()
        browser.close()

    srv.shutdown()
    passed = sum(1 for _, c, _ in results if c)
    print("\n======== UI 验收: %d 通过 / %d 失败 ========" % (passed, len(results) - passed))
    for n, c, d in results:
        if not c:
            print("FAIL:", n, "|", d)
    if bad_urls:
        print("=== 4xx/5xx 请求 ===")
        for u in sorted(set(bad_urls)):
            print("  ", u)
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(2)
