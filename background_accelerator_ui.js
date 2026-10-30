(() => {
  const memoryFloor = 160 * 1024 * 1024;
  const cpuFloor = 5;
  const api = () => window.pywebview?.api;
  const contexts = [
    {key:'widget', scan:'accelerator-process-scan', status:'accelerator-process-status', actions:'accelerator-process-actions', summary:'accelerator-process-summary', list:'accelerator-process-list', select:'accelerator-process-select-all', clear:'accelerator-process-clear', run:'accelerator-process-run'},
    {key:'process', scan:'process-background-scan', status:'process-background-status', actions:'process-background-actions', summary:'process-background-summary', list:'process-background-list', select:'process-background-select-all', clear:'process-background-clear', run:'process-background-run'}
  ];

  function normalizeSnapshot(rows, visiblePids) {
    if (!Array.isArray(rows)) throw new Error('运行程序列表暂不可用');
    if (!Array.isArray(visiblePids)) throw new Error('无法识别可见窗口，为避免误关程序，本次不生成建议名单。');
    const visibleSet = new Set(visiblePids.map(Number).filter(Number.isInteger));
    const executableKey = value => typeof value==='string'?value.replace(/\//g,'\\').trim().toLowerCase():'';
    const visibleExecutables = new Set(rows.filter(item=>item&&Number.isInteger(item.pid)&&visibleSet.has(item.pid))
      .map(item=>executableKey(item.exe)).filter(Boolean));
    const result = {items:[], skipped:0, protected:0, visible:0};
    for (const item of rows) {
      if (!item || typeof item !== 'object') { result.skipped++; continue; }
      const identityValid = Number.isInteger(item.pid) && item.pid > 0 &&
        typeof item.started === 'number' && Number.isFinite(item.started) && item.started > 0;
      const fieldsValid = typeof item.name === 'string' && item.name.trim().length > 0 &&
        typeof item.exe === 'string' && item.exe.trim().length > 0 &&
        typeof item.protected === 'boolean' &&
        typeof item.memory === 'number' && Number.isFinite(item.memory) && item.memory >= 0 &&
        typeof item.cpu === 'number' && Number.isFinite(item.cpu) && item.cpu >= 0 &&
        (item.cpuReady === undefined || typeof item.cpuReady === 'boolean');
      // Missing identity or safety data means this row can never be offered for closure.
      if (!identityValid || !fieldsValid) { result.skipped++; continue; }
      const exeKey=executableKey(item.exe),basename=exeKey.split('\\').pop()||'';
      const isLxUltra=basename.startsWith('lxultra');
      const isWindowsExecutable=/^[a-z]:\\windows\\/i.test(exeKey);
      if (item.protected||isLxUltra||isWindowsExecutable) { result.protected++; continue; }
      if (visibleSet.has(item.pid)||visibleExecutables.has(exeKey)) { result.visible++; continue; }
      if (item.memory >= memoryFloor || (item.cpuReady !== false && item.cpu >= cpuFloor))
        result.items.push({...item, selected:true});
    }
    result.items.sort((a,b)=>b.memory-a.memory);
    return result;
  }
  window.LxBackgroundAccelerator = {normalizeSnapshot,scanWidget:()=>scan(contexts[0])};

  function byId(id) { return document.getElementById(id); }
  function escape(value) { const node=document.createElement('span'); node.textContent=String(value??''); return node.innerHTML; }
  function bytes(value) {
    let number=Math.max(0,Number(value)||0), unit=0;
    const units=['B','KB','MB','GB','TB'];
    while(number>=1024&&unit<units.length-1){number/=1024;unit++}
    return `${number>=10?number.toFixed(0):number.toFixed(1)} ${units[unit]}`;
  }
  function cpu(value) {
    const number=Number(value);
    if(!Number.isFinite(number)||number<0)return '—';
    return `${number<0.1&&number>0?'<0.1':number.toFixed(number%1?1:0)}%`;
  }
  function setStatus(context,message,state='info') {
    const node=byId(context.status);
    node.textContent=message;
    node.dataset.state=state;
    node.setAttribute('aria-busy',String(state==='busy'));
  }
  function render(context) {
    const selected=context.items.filter(item=>item.selected);
    const total=context.items.reduce((sum,item)=>sum+Math.max(0,Number(item.memory)||0),0);
    byId(context.actions).hidden=!context.scanned;
    byId(context.summary).textContent=context.scanned?`建议 ${context.items.length} 个 · 已勾选 ${selected.length} 个 · ${bytes(total)} 内存占用`:'';
    byId(context.run).disabled=context.busy||!selected.length;
    const quickButton=context.key==='widget'?byId('accelerator-quick-start'):null;
    if(quickButton)quickButton.disabled=context.busy||(context.scanned&&!selected.length);
    byId(context.select).disabled=context.busy||!context.items.length;
    byId(context.clear).disabled=context.busy||!selected.length;
    const list=byId(context.list);
    list.innerHTML=context.items.length?context.items.map((item,index)=>`<label class="background-accelerator-row"><input type="checkbox" data-bg-index="${index}" ${item.selected?'checked':''} ${context.busy?'disabled':''}><span><strong>${escape(item.name)} · PID ${item.pid}</strong><small>内存 ${bytes(item.memory)} · CPU ${cpu(item.cpu)} · ${escape(item.exe)}</small></span></label>`).join(''):context.scanned?'<p class="note">没有发现达到建议条件的无窗口后台程序。</p>':'';
    list.querySelectorAll('[data-bg-index]').forEach(box=>box.addEventListener('change',()=>{
      const item=context.items[Number(box.dataset.bgIndex)];
      if(item)item.selected=box.checked;
      render(context);
    }));
  }
  async function scan(context) {
    if(context.busy)return;
    if(!api()){setStatus(context,'后台服务暂不可用，请重新打开程序。','warn');return}
    context.busy=true;
    context.scanned=false;
    context.items=[];
    const button=byId(context.scan),label=button.textContent;
    button.disabled=true;button.textContent='正在扫描…';
    render(context);
    setStatus(context,'正在检查可见窗口和后台程序占用…','busy');
    try {
      const [rows,windows]=await Promise.all([api().processes(),api().process_background_windows()]);
      if(!windows?.ok||!Array.isArray(windows.visiblePids))throw new Error(windows?.error||'无法识别可见窗口，为避免误关程序，本次不生成建议名单。');
      const normalized=normalizeSnapshot(rows,windows.visiblePids);
      context.items=normalized.items;
      context.scanned=true;
      render(context);
      const skipped=normalized.skipped?`已跳过 ${normalized.skipped} 条信息不全的记录（不会对其执行操作）。`:'';
      const status=context.items.length
        ?`扫描完成：已勾选 ${context.items.length} 个无可见窗口且内存达到 160 MB 或 CPU 达到 5% 的当前用户程序；${skipped}请核对名单，取消要保留的程序，再点“预览并加速”。确认前不会关闭任何程序。`
        :`扫描完成，没有发现达到建议条件的无窗口后台程序。${skipped}`;
      setStatus(context,status,'complete');
    } catch(error) {
      console.error('[LxUltra] background acceleration scan failed',error);
      setStatus(context,error?.message||'扫描失败；没有程序被关闭。','warn');
    } finally {
      context.busy=false;
      button.disabled=false;button.textContent=label;
      render(context);
    }
  }
  async function accelerate(context) {
    if(context.busy)return;
    const selected=context.items.filter(item=>item.selected);
    if(!selected.length)return;
    if(!window.LxProcessCleanup?.preview){setStatus(context,'关闭预览暂不可用；没有程序被关闭。','warn');return}
    context.busy=true;
    byId(context.run).disabled=true;
    setStatus(context,'正在复核名单。确认前不会关闭程序…','busy');
    try {
      const result=await window.LxProcessCleanup.preview(selected.map(item=>({pid:item.pid,started:Number(item.started)})),message=>setStatus(context,message,message.includes('失败')?'warn':'info'));
      if(result?.ok&&result.closed>0){context.items=[];context.scanned=false;render(context);setStatus(context,`已完成：关闭 ${result.closed} 个后台程序，未关闭 ${result.failed||0} 个。`,'complete')}
      else if(result?.cancelled){setStatus(context,'已取消，程序未关闭。','info')}
      else if(result?.ok&&result.failed>0){setStatus(context,`未能关闭 ${result.failed} 个所选程序；可在程序管理结果中查看原因。`,'warn')}
      else if(result?.ok&&result.closed===0){setStatus(context,'没有可关闭的所选后台程序。','info')}
      else if(!result?.ok)setStatus(context,result?.error||'加速未完成；没有确认以外的程序被关闭。','warn');
    } catch(error) {
      console.error('[LxUltra] background acceleration failed',error);
      setStatus(context,'加速请求中断，请刷新后台程序列表确认；不会自动重试。','warn');
    } finally {
      context.busy=false;
      render(context);
    }
  }

  const quickStart=byId('accelerator-quick-start');
  if(quickStart)quickStart.addEventListener('click',async()=>{
    const context=contexts[0];
    if(context.busy)return;
    // Preserve a user's reviewed selection; a fresh page first obtains a scan.
    if(!context.scanned)await scan(context);
    if(context.scanned&&context.items.some(item=>item.selected))await accelerate(context);
  });

  contexts.forEach(context=>{
    const scanButton=byId(context.scan);
    if(!scanButton)return;
    context.items=[];context.scanned=false;context.busy=false;
    scanButton.addEventListener('click',()=>scan(context));
    byId(context.run).addEventListener('click',()=>accelerate(context));
    byId(context.select).addEventListener('click',()=>{context.items.forEach(item=>item.selected=true);render(context)});
    byId(context.clear).addEventListener('click',()=>{context.items.forEach(item=>item.selected=false);render(context)});
  });
})();
