"""Exercise disk clone target selection with disposable, in-memory disk fixtures."""
from pathlib import Path
from playwright.sync_api import sync_playwright

root = Path(__file__).resolve().parent
with sync_playwright() as p:
    browser = p.chromium.launch(channel='msedge', headless=True)
    page = browser.new_page(viewport={'width': 1280, 'height': 800})
    page.set_content('<style>' + (root / 'partition_ui.css').read_text(encoding='utf-8') + '</style><div id="partition-root"></div>')
    page.evaluate('''() => {
      const part=(letter)=>({number:1,letter,fs:'NTFS',size:1000000000,used:100000000,offset:0,label:letter});
      const disks=[{number:0,name:'Source',size:1000000000,style:'GPT',health:'Healthy',partitions:[part('D')]},
                   {number:1,name:'Target',size:1000000000,style:'GPT',health:'Healthy',partitions:[part('E')]}];
      window.pywebview={api:{partition_disks:async()=>({ok:true,disks,admin:true}),
        partition_health:async()=>({ok:true,health:{}})}};
    }''')
    page.add_script_tag(content=(root / 'partition_ui.js').read_text(encoding='utf-8'))
    page.evaluate('window.LxPartition.enter()')
    page.wait_for_selector('.pt-map-disk[data-disk="0"]')
    page.locator('.pt-map-disk[data-disk="0"]').click()
    page.locator('[data-act="menu"][data-menu="clone"]').click()
    data_actions = page.locator('.pt-menu.open [data-act="clone-data"]')
    assert data_actions.count() == 1
    assert page.locator('.pt-menu.open [data-act="clone-disk"]').count() == 0
    assert page.locator('.pt-menu.open [data-act="clone-part"]').count() == 0
    data_actions.click()
    assert '源硬盘 0' in page.inner_text('.pt-clone-prompt')
    page.locator('.pt-map-disk[data-disk="0"]').click()
    assert page.locator('.pt-clone-prompt').count() == 1 and not page.is_visible('#pt-mask.open')
    page.locator('.pt-map-segment[data-disk="1"][data-part="1"]').click()
    text = page.inner_text('#pt-modal-body')
    assert '源：硬盘 0' in text and '目标：硬盘 1' in text
    page.locator('[data-act="modal-ok"]').click()
    assert '克隆 盘0 → 盘1' in page.inner_text('#pt-pending-host')
    page.locator('[data-act="clear-queue"]').click()

    page.locator('tr.pt-part-row[data-disk="0"][data-part="1"]').click()
    page.locator('[data-act="menu"][data-menu="clone"]').click()
    page.locator('.pt-menu.open [data-act="clone-data"]').click()
    assert '源分区 D:' in page.inner_text('.pt-clone-prompt')
    page.locator('.pt-map-segment[data-disk="0"][data-part="1"]').click()
    assert page.locator('.pt-clone-prompt').count() == 1
    page.locator('.pt-map-segment[data-disk="1"][data-part="1"]').click()
    text = page.inner_text('#pt-modal-body')
    assert '源：盘0' in text and 'D:' in text and '目标：盘1' in text and 'E:' in text
    page.locator('[data-act="modal-ok"]').click()
    assert '克隆分区 盘0 分区1 → 盘1 分区1' in page.inner_text('#pt-pending-host')
    print('PASS locked disk source, rejected same disk, selected target and queued correct direction')
    browser.close()
