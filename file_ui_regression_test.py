"""Browser regressions for the file manager hover row and cleanup defaults."""
from pathlib import Path
import re
import sys

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parent
HTML = (ROOT / "index.html").read_text(encoding="utf-8")
JS = (ROOT / "file_ui.js").read_text(encoding="utf-8")
CSS = (ROOT / "file_ui.css").read_text(encoding="utf-8")
PARTITION_CSS = (ROOT / "partition_ui.css").read_text(encoding="utf-8")
STYLE_BLOCKS = re.findall(r"<style[^>]*>(.*?)</style>", HTML, flags=re.S | re.I)
assert len(STYLE_BLOCKS) >= 2, "index.html must keep the two app style blocks"


def extract_function(source, signature):
    start = source.index(signature)
    brace = source.index("{", start)
    depth = 0
    quote = None
    escaped = False
    for pos in range(brace, len(source)):
        char = source[pos]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in "'\"`":
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[start:pos + 1]
    raise AssertionError(f"unclosed function: {signature}")


def main():
    results = []

    def check(name, passed, detail=""):
        results.append(bool(passed))
        print(("PASS " if passed else "FAIL ") + name + (f" | {detail}" if detail else ""))

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        css = "".join(f"<style>{part}</style>" for part in
                       (STYLE_BLOCKS[0], CSS, PARTITION_CSS, STYLE_BLOCKS[-1]))
        page.set_content(
            "<!doctype html><html lang='zh-CN' data-appearance='light'><head>"
            + css + "<style>:root[data-appearance='light']{--bg:#F5F6F8;--panel:#FFFFFF;--panel2:#F3F4F6;--line:#D9DCE1;--text:#202226;--muted:#616771;--accent:#8A641F;--control-radius:12px;--card-radius:14px;--button-hover-bg:#EAE7DF;--button-hover-border:#A99A79;--button-hover-shadow:0 7px 18px rgba(52,42,22,.14)}</style>"
            + "</head><body>"
            + "<button id='reference-primary' class='btn primary'>查看优化项</button>"
            + "<button id='reference-default' class='btn'>重新检测设备</button>"
            + "<nav class='settings-section-nav'><button id='settings-general' class='btn active'>常规设置</button><button id='settings-version' class='btn'>版本更新</button></nav>"
            + "<div id='file-manager-root'></div>"
            + "<div id='ptfixture' class='pt-ribbon'>"
            + "<button id='pt-submit' class='pt-ribbon-submit'>提交</button>"
            + "<button id='pt-undo' class='pt-icon-btn'>↶</button>"
            + "<div class='pt-menu'><button id='pt-menu' class='pt-ribbon-btn'>克隆</button></div>"
            + "</div></body></html>"
        )
        page.add_script_tag(content=JS)
        page.wait_for_selector(".fm-tabs button")

        def style_signature(selector):
            return page.locator(selector).first.evaluate("""el => {
              const s=getComputedStyle(el);
              return [s.backgroundColor,s.color,s.borderTopColor,s.borderTopWidth,s.borderRadius];
            }""")

        for selector, reference, label in (
            (".fm-tabs button.active", "#reference-primary", "file main selected button matches reference"),
            (".fm-tabs button:not(.active)", "#reference-default", "file main idle button matches reference"),
            (".fm-subtabs button.active", "#reference-primary", "file subtab selected button matches reference"),
            (".fm-subtabs button:not(.active)", "#reference-default", "file subtab idle button matches reference"),
            ("#pt-submit", "#reference-primary", "partition submit button matches reference"),
            ("#pt-menu", "#reference-default", "partition tool buttons match reference"),
            ("#pt-undo", "#reference-default", "partition undo button matches reference"),
        ):
            actual, expected = style_signature(selector), style_signature(reference)
            check(label, actual == expected, f"actual={actual}, reference={expected}")

        for selector in ("#pt-menu", "#pt-undo", "#pt-submit"):
            button = page.locator(selector)
            button.scroll_into_view_if_needed()
            before = button.bounding_box()
            button.hover()
            page.wait_for_timeout(180)
            after = button.bounding_box()
            transform = button.evaluate("el => getComputedStyle(el).transform")
            stable = abs(after["y"] - before["y"]) < 0.1 and transform == "none"
            check(f"{selector} hover does not lift or shift the toolbar button", stable,
                  f"y {before['y']:.1f}->{after['y']:.1f}, transform={transform}")

        for selector in (".fm-tabs button:not(.active)", ".fm-subtabs button:not(.active)",
                         "#settings-general", "#settings-version"):
            button = page.locator(selector).first
            button.scroll_into_view_if_needed()
            before = button.bounding_box()
            button.hover()
            page.wait_for_timeout(220)
            after = button.bounding_box()
            transform = button.evaluate("el => getComputedStyle(el).transform")
            stable = abs(after["y"] - before["y"]) < 0.1 and transform == "none"
            check(f"{selector} hover does not lift or shift the tab", stable,
                  f"y {before['y']:.1f}->{after['y']:.1f}, transform={transform}")

        software = page.locator('[data-junk-cat="software"]')
        check("common software cache starts unchecked", not software.is_checked())
        main_tabs = page.locator(".fm-tabs [data-fm-tab]").evaluate_all(
            "nodes => nodes.map(node => [node.dataset.fmTab, node.textContent.trim()])")
        check("software management follows file organizer in the first row",
              main_tabs[:2] == [["organize", "文件整理"], ["software", "软件管理"]],
              repr(main_tabs[:3]))
        cleanup_tabs = page.locator(".fm-subtabs [data-fm-subtab]").evaluate_all(
            "nodes => nodes.map(node => [node.dataset.fmSubtab, node.textContent.trim()])")
        check("garbage cleanup is named clearly and precedes file shredding",
              ["junk", "垃圾清理"] in cleanup_tabs and ["shred", "文件粉碎"] in cleanup_tabs and
              cleanup_tabs.index(["junk", "垃圾清理"]) < cleanup_tabs.index(["shred", "文件粉碎"]),
              repr(cleanup_tabs))
        check("large-file migration has a file icon slot in rendered rows",
              "fm-file-icon" in JS and "fm-file-icon" in CSS)
        check("garbage cleanup results have a visible category icon",
              "fm-junk-icon" in JS and "fm-junk-icon" in CSS)
        check("software management section and refresh action are present",
              page.locator("#fm-section-software #fm-software-refresh").count() == 1 and
              page.locator("#fm-section-software #fm-software-list").count() == 1)
        check("software management tab opens the installed software list",
              "software" in JS and "fm_software_list" in JS and "fm_software_uninstall" in JS)
        page.mouse.move(0, 0)
        page.screenshot(path=str(ROOT / "shots" / "10_file_tabs_style.png"), full_page=True)

        page.set_content("""<!doctype html><main>
          <header id='page-title'></header><p id='page-desc'></p>
          <nav id='nav'><button data-pane='repair'>电脑修复</button></nav>
          <section id='network' class='pane'></section>
          <section id='systemrepair' class='pane'></section>
          <section id='other' class='pane'></section>
        </main>""")
        show_pane = extract_function(HTML, "function showPane(id)")
        page.add_script_tag(content="""
          const pages={repair:['电脑修复','系统与网络修复']};
          // showPane delegates the scroll reset to the main-page helper.
          // Keep this focused test harness self-contained; nav_test.py exercises
          // the real reset behavior against the installed page.
          function resetPaneScroll(){}
          function setText(id,value){document.getElementById(id).textContent=value}
          function pollMaintenance(){window.__repairPolls=(window.__repairPolls||0)+1}
        """ + show_pane + "window.__showPane=showPane;")
        page.evaluate("window.__showPane('repair')")
        panes = page.locator("#network.active, #systemrepair.active").count()
        check("Computer Repair opens both system and network panels", panes == 2 and
              page.locator("#nav [data-pane='repair'].active").count() == 1 and
              page.evaluate("window.__repairPolls") == 1,
              f"active panels={panes}")
        page.evaluate("""() => document.body.insertAdjacentHTML('beforeend',
          '<select id="storage-drive"><option value="X:">旧盘符</option></select>'
          + '<p id="storage-status"></p>')""")
        page.add_script_tag(content="""
          function $(id){return document.getElementById(id)}
          function api(){return window.pywebview.api}
          function escapeHtml(value){return String(value)}
        """ + extract_function(HTML, "async function loadStorageDrives()"))
        page.evaluate("window.pywebview={api:{storage_drives:async()=>({ok:false,error:'磁盘列表暂不可用'})}}")
        storage_result = page.evaluate("""async () => {
          try { return {ok:await loadStorageDrives(), status:document.getElementById('storage-status').textContent,
            selected:document.getElementById('storage-drive').value}; }
          catch(error) { return {error:String(error)}; }
        }""")
        check("failed disk list request shows an error without breaking file cleanup",
              storage_result.get("ok") is False and "失败" in storage_result.get("status", "")
              and storage_result.get("selected") == "all", repr(storage_result))
        browser.close()

    nav = HTML.split('id="nav"', 1)[1].split("</nav>", 1)[0]
    pane_ids = set(re.findall(r'<section\b[^>]*class="pane[^\"]*"[^>]*\bid="([^"]+)"', HTML))
    check("repair navigation has one Computer Repair entry",
          nav.count('data-pane="repair"') == 1 and
          'data-pane="network"' not in nav and 'data-pane="systemrepair"' not in nav)
    check("both repair feature panels remain available",
          {"network", "systemrepair"}.issubset(pane_ids))

    check("process and startup management share one sidebar entry",
          nav.count('data-pane="programs"') == 1 and
          'data-pane="processes"' not in nav and 'data-pane="startup"' not in nav)
    check("program management keeps both internal functions",
          'id="program-management-tabs"' in HTML and
          'data-program-view="startup"' in HTML and 'data-program-view="processes"' in HTML)

    print(f"{sum(results)}/{len(results)} checks passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
