/* file:// is a launch page; the operational workbench always uses loopback HTTP. */
(() => {
  'use strict';
  if (location.protocol === 'file:') {
    const entry = document.getElementById('local-entry');
    if (entry) entry.hidden = false;
    const path = decodeURIComponent(new URL('start.command', location.href).pathname);
    const command = "'" + path.replace(/'/g, "'\\''") + "'";
    document.getElementById('entry-command').textContent = command;
    document.getElementById('entry-copy').onclick = async () => {
      const status = document.getElementById('entry-copy-status');
      try { await navigator.clipboard.writeText(command); status.textContent = '已复制'; }
      catch (_) { status.textContent = '请选中上面的命令复制'; }
    };
    return;
  }
  const scripts = ['pitch_core.js', 'pitch.js', 'interactive.js', 'console.js', 'metronome.js', 'calibration_ui.js'];
  let remaining = scripts.length, failed = false;
  function load(name, onload) {
    const script = document.createElement('script');
    // Download dependencies together, while preserving their execution order.
    script.async = false;
    script.src = name;
    script.onload = onload;
    script.onerror = () => {
      failed = true;
      const error = document.getElementById('entry-error');
      error.hidden = false;
      error.textContent = '工作台资源加载失败（' + name + '）。请刷新页面重试；本地使用时请确认 start.command 服务正在运行。';
    };
    document.body.append(script);
  }
  for (const name of scripts) load(name, () => {
    remaining -= 1;
    if (!remaining && !failed) load('app.js', () => {});
  });
})();
