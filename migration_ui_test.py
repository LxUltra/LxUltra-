"""Source lock and target confirmation click-through with in-memory fixtures only."""
from pathlib import Path
from playwright.sync_api import sync_playwright

root = Path(__file__).resolve().parent
shots = root / "shots"
shots.mkdir(exist_ok=True)
with sync_playwright() as pw:
    browser = pw.chromium.launch(channel="msedge", headless=True)
    context = browser.new_context(viewport={"width": 1440, "height": 900},
                                  record_video_dir=str(shots),
                                  record_video_size={"width": 1440, "height": 900})
    page = context.new_page()
    theme = ":root{--bg:#F5F6F8;--side:#ECEEF1;--panel:#FFFFFF;--panel2:#F3F4F6;" + \
            "--line:#D9DCE1;--text:#202226;--muted:#616771;--accent:#8A641F;" + \
            "--good:#2F7654;--amber:#875B12;--bad:#B4444B}" + \
            "body{margin:0;padding:18px;background:var(--bg);color:var(--text);" + \
            "font-family:'Microsoft YaHei UI',sans-serif}"
    page.set_content('<style>' + theme + (root / "partition_ui.css").read_text(encoding="utf-8") +
                     '</style><div id="partition-root"></div>')
    page.evaluate("""() => {
      const part=(n,l,fs,size,used,extra={})=>Object.assign(
        {number:n,letter:l,fs,size,used,free:size-used,offset:n*100000000000,
         guid:'part-'+n+'-'+l,label:l,bitlocker:'FullyDecrypted'},extra);
      const disks=[
        {number:1,name:'SOURCE',size:2000000000000,style:'GPT',isSystemDisk:true,
         partitions:[part(2,'','FAT32',200000000,0,{isEFI:true,isSystem:true}),
                     part(3,'C','NTFS',1000000000000,150000000000,{isBoot:true})]},
        {number:0,name:'TARGET',size:2000000000000,style:'GPT',isSystemDisk:false,
         partitions:[part(13,'','FAT32',100000000,0,{isEFI:true}),
                     part(15,'L','NTFS',420000000000,80000000000)]},
        {number:2,name:'SCRATCH',size:2000000000000,style:'GPT',isSystemDisk:false,
         partitions:[part(3,'G','NTFS',900000000000,100000000000)]}];
      window.__submitted=null; window.__plans=[]; window.__jobStatusCalls=0; window.__resolvePlan=null;
      window.__cancelCalls=[]; window.__cancelRequested=false;
      window.pywebview={api:{
        partition_disks:async()=>({ok:true,disks,admin:true}),
        partition_health:async()=>({ok:true,health:{}}),
        partition_migration_plan:(d,p,s)=>{
          window.__plans.push({d,p,s});
          return new Promise(resolve=>{window.__resolvePlan=()=>resolve({ok:true,token:'test-token',plan:{source:{disk:1,part:3},
            target:{disk:0,part:15,letter:'L',size:420000000000,offset:1500000000000,
              disk_unique_id:'target-unique-id',part_guid:'target-part-guid'},
            scratch:{letter:'G'},new_efi_size:209715200},
            cloud_files:{count:2,total_bytes:1000000,manual_count:0,
              samples:['C:/Users/test/OneDrive/report.docx']},
            reserved_names:[{relative:'\\Users\\test\\.qianfan\\workspace\\nul',
              path:'C:\\\\Users\\\\test\\\\.qianfan\\\\workspace\\\\nul',directory:false,size:0}]});});
        },
        partition_job_submit:(ops)=>{window.__submitted=ops;return new Promise(resolve=>{
          window.__resolveSubmit=()=>resolve({ok:true,job_id:'mock'});
        });},
        partition_job_cancel:async jobId=>{
          window.__cancelCalls.push(jobId);window.__cancelRequested=true;return {ok:true};
        },
        partition_job_status:async()=>{
          window.__jobStatusCalls++;
          if (window.__jobStatusCalls <= 2) return {ok:true,state:'running',current:1,total:1,migration:true,
            progress:'正在执行第 1/1 项:migrate_system',results:[]};
          if (window.__jobStatusCalls <= 3) return {ok:true,state:'running',current:1,total:1,migration:true,
            phase:'capture',percent:15,cancelable:true,cancel_requested:window.__cancelRequested,
            progress:'系统迁移: capture (15% 阶段进度)',results:[]};
          if (window.__jobStatusCalls === 4) return {ok:true,state:'running',current:1,total:1,migration:true,
            phase:'capture',percent:27,estimated:true,detail:'映像已写入 18.0 GB · 估算',
            cancelable:!window.__cancelRequested,cancel_requested:window.__cancelRequested,
            progress:'系统迁移: capture (27% 阶段进度)',results:[]};
          if (window.__jobStatusCalls <= 7) return {ok:true,state:'running',current:1,total:1,migration:true,
            phase:'capture',percent:34,estimated:true,detail:'映像已写入 28.0 GB · 估算',
            cancelable:!window.__cancelRequested,cancel_requested:window.__cancelRequested,
            progress:'系统迁移: capture (34% 阶段进度)',results:[]};
          return {ok:true,state:'error',current:1,total:1,migration:true,
            phase:'hydrate_cloud',percent:7,
            progress:'系统迁移: hydrate_cloud (7% 阶段进度)',results:[{ok:false,type:'migrate_system',
              op:{type:'migrate_system',disk:0,part:15,letter:'L',source_disk:1,source_part:3},
              message:'OneDrive 文件下载失败'}]};
        }
      }};
    }""")
    page.add_script_tag(content=(root / "partition_ui.js").read_text(encoding="utf-8"))
    page.evaluate("window.LxPartition.enter()")
    page.wait_for_selector('tr.pt-part-row[data-disk="1"][data-part="3"]')
    for menu, action in [
        ('convert', 'convert-mbr'), ('recover', 'op-scanfix'),
        ('erase', 'op-wipe'), ('test', 'op-bench'),
        ('test', 'op-health'), ('tools', 'op-defrag'),
    ]:
        page.locator('[data-act="menu"][data-menu="%s"]' % menu).click()
        page.locator('.pt-menu.open [data-act="%s"]' % action).click()
        assert page.locator('#pt-modal-body [data-field="pick"]').count() == 1, action
        assert page.locator('#pt-modal-body [data-act="modal-ok"]').is_disabled(), action
        page.locator('#pt-modal-body [data-act="modal-cancel"]').click()
    assert page.locator('#pt-pending-host .pt-opchip').count() == 0
    page.locator('[data-act="menu"][data-menu="clone"]').click()
    page.locator('.pt-menu.open [data-act="clone-data"]').click()
    assert '选择源' in page.inner_text('#pt-modal-body')
    page.locator('[data-field="pick"]').select_option('disk:2')
    page.locator('[data-act="modal-ok"]').click()
    assert '选择目标' in page.inner_text('#pt-modal-body')
    assert '硬盘 2' in page.inner_text('#pt-modal-body')
    page.locator('[data-field="pick"]').select_option('disk:0')
    page.locator('[data-act="modal-ok"]').click()
    assert '确认克隆硬盘' in page.inner_text('#pt-modal-body')
    page.locator('[data-act="modal-ok"]').click()
    assert page.locator('#pt-pending-host .pt-opchip').count() == 1
    page.locator('#pt-pending-host [data-act="clear-queue"]').click()
    page.locator('[data-act="menu"][data-menu="clone"]').click()
    page.locator('.pt-menu.open [data-act="migrate-system"]').click()
    assert '源：当前 Windows C:' in page.inner_text('#pt-modal-body')
    assert page.locator('[data-act="modal-ok"]').is_disabled()
    page.locator('[data-field="pick"]').select_option('part:0:15')
    page.locator('[data-act="modal-ok"]').click()
    assert '暂存卷' in page.inner_text('#pt-modal-body')
    assert page.locator('tr.pt-part-row.sel[data-disk="0"][data-part="15"]').count() == 1
    page.locator('[data-field="scratch"]').select_option('G')
    page.locator('[data-act="modal-ok"]').click()
    page.wait_for_selector('#pt-modal-body .pt-modal-feedback.is-busy', timeout=3000)
    assert '正在检查' in page.locator('#pt-modal-body [data-act="modal-ok"]').inner_text()
    assert page.locator('#pt-modal-body [data-act="modal-ok"]').is_disabled()
    page.wait_for_function("""() => {
      const el=document.querySelector('#pt-modal-body .pt-modal-elapsed');
      return el && /00:0[1-9]/.test(el.textContent);
    }""", timeout=5000)
    page.screenshot(path=str(shots / "system_migration_preflight_wait.png"))
    page.evaluate('window.__resolvePlan()')
    page.wait_for_function("document.querySelector('#pt-modal-body').textContent.includes('确认系统迁移')")
    assert page.evaluate('window.__plans') == [{'d': 0, 'p': 15, 's': 'G'}]
    assert '只会删除上述目标分区' in page.inner_text('#pt-modal-body')
    assert 'Windows 保留名条目' in page.inner_text('#pt-modal-body')
    assert 'C:\\Users\\test\\.qianfan\\workspace\\nul' in page.inner_text('#pt-modal-body')
    page.screenshot(path=str(shots / "system_migration_review_styled.png"))
    assert page.locator('#pt-modal-body [data-act="modal-ok"]').is_disabled()
    assert page.locator('#pt-pending-host .pt-opchip').count() == 0
    page.locator('[data-field="confirm_target"]').check()
    assert '2 个' in page.inner_text('#pt-modal-body')
    assert '1 MB' in page.inner_text('#pt-modal-body')
    assert page.locator('#pt-modal-body [data-act="modal-ok"]').is_disabled()
    page.locator('[data-field="confirm_cloud_download"]').check()
    assert page.locator('#pt-modal-body [data-act="modal-ok"]').is_disabled()
    page.locator('[data-field="confirm_reserved_names"]').check()
    assert page.locator('#pt-modal-body [data-act="modal-ok"]').is_enabled()
    page.locator('[data-act="modal-ok"]').click()
    assert '系统迁移 C: → 硬盘 0 分区15' in page.inner_text('#pt-pending-host')
    page.locator('#pt-pending-host [data-act="submit"]').click()
    page.locator('#pt-modal-body [data-act="modal-ok"]').click()
    page.wait_for_function('window.__submitted !== null')
    assert page.evaluate('window.__submitted') == [
        {'type':'migrate_system','disk':0,'part':15,'token':'test-token','confirmed':True,
         'download_cloud_files':True,'skip_reserved_names':True}]
    assert page.locator('.pt-ribbon-submit').is_disabled()
    assert '提交中' in page.locator('.pt-ribbon-submit').inner_text()
    page.wait_for_selector('#pt-progress-host .pt-progress-activity', timeout=3000)
    assert '正在准备 0%' in page.locator('#pt-progress-host .pt-progress-title').inner_text()
    assert '任务 1/1' in page.inner_text('#pt-progress-host')
    assert page.locator('#pt-progress-host .pt-progress-bar').get_attribute('style') == 'width:0%'
    assert '启动' in page.inner_text('#pt-progress-host')
    page.wait_for_function("""() => {
      const el=document.querySelector('#pt-progress-host .pt-progress-elapsed');
      return el && /00:0[1-9]/.test(el.textContent);
    }""", timeout=5000)
    page.screenshot(path=str(shots / "system_migration_starting_wait.png"))
    page.evaluate('window.__resolveSubmit()')
    page.wait_for_selector('#pt-progress-host .pt-progress-activity')
    page.wait_for_function("""() => {
      const el=document.querySelector('#pt-progress-host .pt-progress-title');
      return el && el.textContent.includes('创建系统映像 15%');
    }""", timeout=8000)
    assert '当前阶段处理中' in page.inner_text('#pt-progress-host .pt-progress-activity')
    assert '已用时 00:00' in page.inner_text('#pt-progress-host .pt-progress-activity')
    assert page.locator('#pt-progress-host .pt-progress-bar').get_attribute('style') == 'width:15%'
    assert page.locator('#pt-progress-host [data-act="cancel-job"]').is_enabled()
    assert page.locator('#pt-progress-host .pt-progress-activity-track').evaluate(
        "el=>getComputedStyle(el.firstElementChild).animationName") == 'pt-phase-sweep'
    page.wait_for_function("""() => {
      const el=document.querySelector('#pt-progress-host .pt-progress-title');
      return el && el.textContent.includes('创建系统映像 27%');
    }""", timeout=5000)
    assert page.locator('#pt-progress-host .pt-progress-bar').get_attribute('style') == 'width:27%'
    assert '估算' in page.locator('#pt-progress-host .pt-progress-title').inner_text()
    assert '18.0 GB' in page.inner_text('#pt-progress-host .pt-progress-text')
    page.wait_for_function("""() => {
      const el=document.querySelector('#pt-progress-host .pt-progress-title');
      return el && el.textContent.includes('创建系统映像 34%');
    }""", timeout=5000)
    assert page.locator('#pt-progress-host .pt-progress-bar').get_attribute('style') == 'width:34%'
    page.locator('#pt-progress-host [data-act="cancel-job"]').click()
    assert page.evaluate('window.__cancelCalls') == ['mock']
    assert page.locator('#pt-progress-host .pt-progress-cancel').is_disabled()
    assert '正在取消' in page.locator('#pt-progress-host .pt-progress-cancel').inner_text()
    page.wait_for_function("""() => {
      const el=document.querySelector('#pt-progress-host .pt-progress-elapsed');
      return el && /00:0[1-9]/.test(el.textContent);
    }""", timeout=5000)
    page.screenshot(path=str(shots / "system_migration_progress_wait.png"))
    page.wait_for_selector('.pt-result.bad')
    result_text = page.inner_text('.pt-result.bad')
    assert '系统迁移 C: → L: · 硬盘 0 分区15' in result_text
    assert 'OneDrive 文件下载失败' in result_text
    assert 'undefined' not in result_text
    assert page.locator('.pt-ribbon-submit').is_disabled()
    assert '已提交' in page.locator('.pt-ribbon-submit').inner_text()
    assert '1 项' not in page.locator('.pt-ribbon-submit').inner_text()
    assert page.evaluate("""() => {
      const host=document.querySelector('#pt-progress-host');
      const items=[...host.parentElement.children];
      return items.indexOf(host) < items.indexOf(document.querySelector('.pt-layout'));
    }""")
    assert page.locator('#pt-progress-host .pt-progress-bar').get_attribute('style') == 'width:7%'
    assert '下载云端文件' in page.inner_text('#pt-progress-host')
    assert page.locator('#pt-progress-host').evaluate("el=>getComputedStyle(el).position") == 'sticky'
    print('PASS system migration source lock, target click, scratch, preview, confirmation, queue and submit')
    video = page.video
    context.close()
    path = shots / "system_migration_ui_demo_styled_20261028.webm"
    Path(video.path()).replace(path)
    print('VIDEO', path)
    browser.close()
