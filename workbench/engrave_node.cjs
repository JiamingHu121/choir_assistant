/* Local, pinned Verovio WASM engraving. No network or experimental edit API. */
'use strict';
const fs = require('node:fs');
const path = require('node:path');

// Emscripten diagnostic printing must not corrupt the JSON result on stdout.
console.log = (...values) => process.stderr.write(values.join(' ') + '\n');

async function main() {
  const modulePath = process.argv[2];
  if (!modulePath || !path.isAbsolute(modulePath)) throw new Error('An absolute local Verovio module path is required');
  const options = JSON.parse(process.argv[3]);
  const xml = fs.readFileSync(0, 'utf8');
  const verovio = require(modulePath);
  if (!verovio.module || typeof verovio.toolkit !== 'function') throw new Error('Unsupported local Verovio module contract');
  await new Promise((resolve, reject) => {
    if (verovio.module.calledRun || verovio.module.runtimeInitialized) return resolve();
    const previous = verovio.module.onRuntimeInitialized;
    verovio.module.onRuntimeInitialized = () => {
      try { if (previous) previous(); resolve(); } catch (error) { reject(error); }
    };
    verovio.module.onAbort = reason => reject(new Error(String(reason)));
  });
  const toolkit = new verovio.toolkit();
  try {
    const version = toolkit.getVersion();
    if (!/^6\.3(?:\.|$)/.test(version)) throw new Error('The configured Verovio WASM must be version 6.3');
    toolkit.setOptions(options);
    if (!toolkit.loadData(xml)) throw new Error('Verovio could not load canonical MusicXML');
    const pages = [];
    for (let page = 1; page <= toolkit.getPageCount(); page++) pages.push(toolkit.renderToSVG(page));
    process.stdout.write(JSON.stringify({version, pages, mei: toolkit.getMEI()}));
  } finally {
    if (typeof toolkit.destroy === 'function') toolkit.destroy();
  }
}
main().catch(error => { process.stderr.write(String(error.stack || error) + '\n'); process.exitCode = 1; });
