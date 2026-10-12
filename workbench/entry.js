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
  const scripts = ['pitch_core.js', 'pitch.js', 'interactive.js', 'console.js', 'metronome.js', 'calibration_ui.js', 'app.js'];
  function load(index) {
    if (index >= scripts.length) return;
    const script = document.createElement('script');
    script.src = scripts[index];
    script.onload = () => load(index + 1);
    script.onerror = () => {
      const error = document.getElementById('entry-error');
      error.hidden = false;
      error.textContent = '工作台资源加载失败（' + scripts[index] + '）。请用 start.command 启动并刷新此页面。';
    };
    document.body.append(script);
  }
  load(0);
})();
