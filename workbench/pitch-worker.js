/* Imported vocal PCM stays in this browser worker; no network upload. */
importScripts('/pitch_core.js');
self.onmessage = event => {
  const {id, samples, sampleRate, options} = event.data;
  try {
    const result = PitchCore.analyzeRecording(new Float32Array(samples), sampleRate, options);
    self.postMessage({id, result});
  } catch (error) {
    self.postMessage({id, error: error.message || String(error)});
  }
};
