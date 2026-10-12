/* Keep functional controls mounted while switching visible console panels. */
(() => {
  'use strict';
  window.initConsole = function initConsole() {
    const host = document.getElementById('workbench-console');
    if (!host) return {show() {}, syncPanels() {}, get current() {return 'selection';}};
    if (host.consoleController) return host.consoleController;
    const tabs = [...host.querySelectorAll('[data-console-tab]')];
    const panels = new Map(tabs.map(tab => [tab.dataset.consoleTab, document.getElementById(tab.getAttribute('aria-controls'))]));
    let current = tabs.find(tab => tab.getAttribute('aria-selected') === 'true')?.dataset.consoleTab || 'selection';
    function show(name, focus = false) {
      if (!panels.has(name)) return false;
      current = name;
      tabs.forEach(tab => {
        const selected = tab.dataset.consoleTab === name;
        tab.classList.toggle('is-current', selected);
        tab.setAttribute('aria-selected', String(selected));
        tab.tabIndex = selected ? 0 : -1;
        const panel = panels.get(tab.dataset.consoleTab);
        if (panel) panel.hidden = !selected;
        if (selected && focus) tab.focus();
      });
      host.dispatchEvent(new CustomEvent('consolechange', {detail: {panel: name}}));
      return true;
    }
    function syncPanels() {
      const selection = document.querySelector('.notation-selection-panel');
      const selectionPanel = panels.get('selection');
      if (selection && selectionPanel && selection.parentElement !== selectionPanel) selectionPanel.append(selection);
      const reviewPanel = panels.get('review');
      for (const selector of ['.notation-editor', '.notation-audit']) {
        const section = document.querySelector(selector);
        if (section && reviewPanel && section.parentElement !== reviewPanel) reviewPanel.append(section);
      }
      const pitch = document.getElementById('pitch-host');
      const pitchPanel = panels.get('pitch');
      if (pitch && pitchPanel && pitch.parentElement !== pitchPanel) pitchPanel.append(pitch);
    }
    tabs.forEach((tab, index) => {
      tab.addEventListener('click', () => show(tab.dataset.consoleTab));
      tab.addEventListener('keydown', event => {
        const next = event.key === 'ArrowRight' ? (index + 1) % tabs.length : event.key === 'ArrowLeft' ? (index - 1 + tabs.length) % tabs.length : event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1 : null;
        if (next === null) return;
        event.preventDefault();
        show(tabs[next].dataset.consoleTab, true);
      });
    });
    const controller = {show, syncPanels, get current() {return current;}};
    host.consoleController = controller;
    syncPanels();
    show(current);
    return controller;
  };
})();
