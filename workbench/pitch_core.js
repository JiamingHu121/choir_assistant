/* Local-only pitch analysis. YIN cumulative mean normalized difference follows
 * de Cheveigne & Kawahara, JASA 111 (2002), DOI:10.1121/1.1458024.
 * Confidence is a periodicity score, not a calibrated probability of correctness.
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  root.PitchCore = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  const finite = (value, fallback) => Number.isFinite(Number(value)) ? Number(value) : fallback;
  const clamp = (value, low, high) => Math.max(low, Math.min(high, value));
  function midiToHz(midi, a4 = 440) {
    const pitch = Number(midi), reference = Number(a4);
    return Number.isFinite(pitch) && reference > 0 && Number.isFinite(reference)
      ? reference * Math.pow(2, (pitch - 69) / 12) : NaN;
  }
  function centsDifference(actual, target) {
    return actual > 0 && target > 0 && Number.isFinite(actual) && Number.isFinite(target)
      ? 1200 * Math.log2(actual / target) : NaN;
  }
  function describePitch(frequency, a4 = 440) {
    if (!(frequency > 0) || !(a4 > 0)) return null;
    const exactMidi = 69 + 12 * Math.log2(frequency / a4);
    const midi = Math.round(exactMidi);
    const names = ['C', 'C#', 'D', 'Eb', 'E', 'F', 'F#', 'G', 'Ab', 'A', 'Bb', 'B'];
    return {midi, exactMidi, name: names[((midi % 12) + 12) % 12] + (Math.floor(midi / 12) - 1),
      cents: (exactMidi - midi) * 100, frequency};
  }

  function detectPitch(samples, sampleRate, options = {}) {
    const fail = (reason, rms = 0, confidence = 0) => ({frequency: null, confidence, rms, reason, voiced: false});
    if (!samples || !Number.isFinite(sampleRate) || sampleRate <= 0 || samples.length < 32) return fail('insufficient-data');
    const maxSamples = Math.max(256, Math.floor(finite(options.maxSamples, 4096)));
    const count = Math.min(samples.length, maxSamples);
    const first = samples.length - count;
    let mean = 0;
    for (let i = first; i < samples.length; i++) {
      if (!Number.isFinite(samples[i])) return fail('invalid-data');
      mean += samples[i];
    }
    mean /= count;
    let energy = 0;
    for (let i = first; i < samples.length; i++) energy += Math.pow(samples[i] - mean, 2);
    const rms = Math.sqrt(energy / count);
    if (rms < Math.max(0, finite(options.minRms, 0.008))) return fail('silence', rms);

    // Block-average decimation limits per-frame work, while attenuating frequencies
    // beyond the singing range. Frequency conversion uses the actual reduced rate.
    const factor = Math.max(1, Math.floor(sampleRate / Math.max(4000, finite(options.targetRate, 12000))));
    const rate = sampleRate / factor;
    const data = new Float64Array(Math.floor(count / factor));
    for (let i = 0; i < data.length; i++) {
      let sum = 0;
      for (let j = 0; j < factor; j++) sum += samples[first + i * factor + j] - mean;
      data[i] = sum / factor;
    }
    const minimum = Math.max(20, finite(options.minFrequency, 55));
    const maximum = Math.min(rate / 2, Math.max(minimum, finite(options.maxFrequency, 1200)));
    const maxLag = Math.min(Math.ceil(rate / minimum) + 2, Math.floor(data.length / 2) - 1);
    if (maxLag < 4 || data.length < 2 * rate / minimum) return fail('insufficient-data', rms);
    const width = data.length - maxLag;
    const difference = new Float64Array(maxLag + 1);
    let cumulative = 0;
    difference[0] = 1;
    for (let lag = 1; lag <= maxLag; lag++) {
      let sum = 0;
      for (let i = 0; i < width; i++) {
        const delta = data[i] - data[i + lag];
        sum += delta * delta;
      }
      cumulative += sum;
      difference[lag] = cumulative > 0 ? sum * lag / cumulative : 1;
    }
    const threshold = clamp(finite(options.threshold, 0.15), 0.01, 0.5);
    let candidate = -1;
    // Search from lag 2, including pitches above maximum, to reject them instead
    // of reporting their subharmonics as a within-range target.
    for (let lag = 2; lag < maxLag; lag++) {
      if (difference[lag] < threshold) {
        while (lag + 1 <= maxLag && difference[lag + 1] < difference[lag]) lag++;
        candidate = lag;
        break;
      }
    }
    if (candidate < 0 || candidate >= maxLag) return fail('aperiodic', rms);
    const refinements = new Map();
    const refinePeriod = lag => {
      if (refinements.has(lag)) return refinements.get(lag);
      const left = difference[lag - 1], middle = difference[lag], right = difference[lag + 1];
      const curvature = left - 2 * middle + right;
      const adjustment = curvature > 0 ? clamp((left - right) / (2 * curvature), -0.5, 0.5) : 0;
      const approximatePeriod = sampleRate * (lag + adjustment) / rate;
      const lower = Math.max(2, Math.floor(approximatePeriod) - 2);
      const upper = Math.min(Math.floor(count / 2) - 1, Math.ceil(approximatePeriod) + 2);
      const originalWidth = count - upper - 1;
      const rawDifference = [];
      let bestLag = Math.round(approximatePeriod), bestValue = Infinity;
      for (let rawLag = lower - 1; rawLag <= upper + 1; rawLag++) {
        let sum = 0;
        for (let i = 0; i < originalWidth; i++) {
          const delta = samples[first + i] - samples[first + i + rawLag];
          sum += delta * delta;
        }
        rawDifference[rawLag] = sum;
        if (rawLag >= lower && rawLag <= upper && sum < bestValue) {bestValue = sum; bestLag = rawLag;}
      }
      const rawLeft = rawDifference[bestLag - 1], rawMiddle = rawDifference[bestLag], rawRight = rawDifference[bestLag + 1];
      const rawCurvature = rawLeft - 2 * rawMiddle + rawRight;
      const rawAdjustment = rawCurvature > 0 ? clamp((rawLeft - rawRight) / (2 * rawCurvature), -0.5, 0.5) : 0;
      const rawValley = Math.max(0, rawCurvature > 0 ? rawMiddle - Math.pow(rawLeft - rawRight, 2) / (8 * rawCurvature) : rawMiddle);
      const refined = {period: bestLag + rawAdjustment, score: rawValley / (2 * rms * rms * originalWidth)};
      refinements.set(lag, refined);
      return refined;
    };
    // A weak fundamental under a strong second harmonic can create an early
    // shallow valley. Only move to a later integer multiple when the periodicity
    // improves substantially; never use target frequency to fold octave errors.
    for (let multiplier = 2; multiplier <= 3; multiplier++) {
      const around = candidate * multiplier;
      if (around + 2 >= maxLag) break;
      let best = around;
      for (let lag = around - 2; lag <= around + 2; lag++) {
        if (difference[lag] < difference[best]) best = lag;
      }
      if (refinePeriod(best).score < refinePeriod(candidate).score * 0.2 && refinePeriod(candidate).score - refinePeriod(best).score > 0.012) {
        candidate = best;
        break;
      }
    }
    const confidence = clamp(1 - difference[candidate], 0, 1);
    if (confidence < clamp(finite(options.minConfidence, 0.8), 0, 1)) return fail('aperiodic', rms, confidence);
    // Refine just a few lags at the original rate. This avoids several cents of
    // interpolation bias on soprano pitches after decimation, without paying the
    // full original-rate YIN cost for every possible lag.
    const frequency = sampleRate / refinePeriod(candidate).period;
    // A 0.01% numerical margin avoids rejecting a tone exactly on a search bound.
    if (frequency < minimum * 0.9999 || frequency > maximum * 1.0001) return fail('out-of-range', rms, confidence);
    return {frequency, confidence, rms, reason: 'voiced', voiced: true};
  }

  function fftSpectrum(samples, sampleRate, options = {}) {
    if (typeof options === 'number') options = {fftSize: options};
    const requested = clamp(Math.floor(finite(options.fftSize, 4096)), 32, 32768);
    const size = Math.pow(2, Math.ceil(Math.log2(requested)));
    const real = new Float64Array(size), imaginary = new Float64Array(size);
    const length = Math.min(samples ? samples.length : 0, size);
    const sourceStart = Math.max(0, (samples ? samples.length : 0) - size);
    let windowSum = 0;
    for (let i = 0; i < size; i++) {
      const weight = 0.5 * (1 - Math.cos(2 * Math.PI * i / (size - 1)));
      real[i] = i < length ? finite(samples[sourceStart + i], 0) * weight : 0;
      windowSum += weight;
    }
    for (let i = 1, j = 0; i < size; i++) {
      let bit = size >> 1;
      while (j & bit) {j ^= bit; bit >>= 1;}
      j ^= bit;
      if (i < j) {[real[i], real[j]] = [real[j], real[i]];}
    }
    for (let width = 2; width <= size; width <<= 1) {
      const theta = -2 * Math.PI / width;
      const stepReal = Math.cos(theta), stepImaginary = Math.sin(theta);
      for (let start = 0; start < size; start += width) {
        let wr = 1, wi = 0;
        for (let j = 0; j < width / 2; j++) {
          const left = start + j, right = left + width / 2;
          const tr = wr * real[right] - wi * imaginary[right];
          const ti = wr * imaginary[right] + wi * real[right];
          real[right] = real[left] - tr;
          imaginary[right] = imaginary[left] - ti;
          real[left] += tr;
          imaginary[left] += ti;
          const nextWr = wr * stepReal - wi * stepImaginary;
          wi = wr * stepImaginary + wi * stepReal;
          wr = nextWr;
        }
      }
    }
    const frequencies = new Float32Array(size / 2 + 1), db = new Float32Array(size / 2 + 1);
    for (let i = 0; i < frequencies.length; i++) {
      frequencies[i] = i * sampleRate / size;
      const amplitude = Math.hypot(real[i], imaginary[i]) * (i === 0 || i === size / 2 ? 1 : 2) / windowSum;
      db[i] = 20 * Math.log10(Math.max(1e-8, amplitude));
    }
    return {frequencies, db, fftSize: size, sampleRate};
  }

  const soundingNote = note => note && note.kind !== 'rest' && !note.grace && Number.isFinite(note.pitch) && Number.isFinite(note.start) && Number.isFinite(note.end) && note.end > note.start;

  function performanceSelection(input, options = {}) {
    const model = Array.isArray(input) ? {notes:input, schemaVersion:options.schemaVersion || 1,
      ...(options.performanceEvents === undefined ? {} : {performanceEvents:options.performanceEvents})} : input || {notes:[]};
    const allNotes = (model.notes || []).filter(soundingNote), lookup = new Map(allNotes.map(note => [note.id,note]));
    const selectedIds = options.noteIds === undefined || options.noteIds === null ? null : new Set(options.noteIds);
    if (selectedIds && [...selectedIds].some(id => !(model.notes || []).some(note => note.id === id))) throw new Error('选区包含失效音符，请刷新曲谱');
    const selected = allNotes.filter(note => !selectedIds || selectedIds.has(note.id));
    const canonical = model.performanceEvents;
    if (canonical === undefined || canonical === null) {
      if (Number(model.schemaVersion || 1) >= 2) throw new Error('规范乐谱缺少演奏事件，请重新生成曲谱');
      // Explicit compatibility for historical note-only callers. Production
      // supplies the whole canonical model and never reinterprets tie markers.
      const events=[];
      for (const note of selected.slice().sort((a,b)=>a.start-b.start || String(a.id).localeCompare(String(b.id)))) {
        const previous = events.find(event => note.performanceId && event.performanceId === note.performanceId && event.pitch === note.pitch && event.part === note.part && note.start <= event.end + 1e-6);
        if (previous) {previous.end=Math.max(previous.end,note.end);previous.noteIds.push(note.id);continue;}
        events.push({...note,id:note.performanceId || `p-${note.id}`,noteIds:[note.id],performanceId:note.performanceId || null});
      }
      return {notes:selected,allNotes,events,eventSource:'legacy-notes'};
    }
    if (!Array.isArray(canonical)) throw new Error('演奏事件格式无效，请重新生成曲谱');
    const members = new Set(), eventIds = new Set();
    for (const event of canonical) {
      if (typeof event.id !== 'string' || eventIds.has(event.id)) throw new Error('演奏事件 ID 无效或重复');
      eventIds.add(event.id);
      if (!Array.isArray(event.noteIds) || !event.noteIds.length || new Set(event.noteIds).size !== event.noteIds.length) throw new Error('演奏事件缺少有效音符成员');
      if (![event.pitch,event.start,event.end].every(Number.isFinite) || event.end <= event.start) throw new Error('演奏事件的音高或时间无效');
      for (const id of event.noteIds) {
        const note=lookup.get(id);
        if (!note || members.has(id)) throw new Error('演奏事件音符成员失效或重复');
        if ((note.performanceId && note.performanceId !== event.id) || note.part !== event.part || note.pitch !== event.pitch) throw new Error('音符与演奏事件不一致，请重新生成曲谱');
        if (note.start < event.start - 1e-6 || note.end > event.end + 1e-6) throw new Error('音符超出演奏事件时间范围');
        members.add(id);
      }
    }
    if (Number(model.schemaVersion || 1) >= 2 && allNotes.some(note => !members.has(note.id))) throw new Error('规范音符缺少演奏事件，请重新生成曲谱');
    const chosen=new Set(selected.map(note=>note.id)), events=[];
    for (const event of canonical) {
      const ids=event.noteIds.filter(id=>chosen.has(id));
      if (!ids.length) continue;
      if (ids.length === event.noteIds.length) {events.push({...event,performanceId:event.id});continue;}
      const clips=[];
      for (const id of ids.slice().sort((a,b)=>lookup.get(a).start-lookup.get(b).start || String(a).localeCompare(String(b)))) {
        const note=lookup.get(id), start=Math.max(event.start,note.start), end=Math.min(event.end,note.end);
        if (end <= start) continue;
        if (clips.length && start <= clips.at(-1).end + 1e-6) {clips.at(-1).end=Math.max(clips.at(-1).end,end);clips.at(-1).noteIds.push(id);}
        else clips.push({start,end,noteIds:[id]});
      }
      clips.forEach((clip,index)=>events.push({...event,...clip,id:`${event.id}::clip${index+1}`,performanceId:event.id,selectionClipped:true}));
    }
    return {notes:selected,allNotes,events:events.sort((a,b)=>a.start-b.start || String(a.id).localeCompare(String(b.id))),eventSource:'performanceEvents'};
  }

  function buildTargets(input, options = {}) {
    const selection=performanceSelection(input,options), all=selection.notes;
    const origin = all.length ? Math.min(...all.map(note => note.start)) : 0;
    const duration = all.length ? Math.max(...all.map(note => note.end)) - origin : 0;
    const transpose = finite(options.transpose, 0), a4 = finite(options.a4, 440);
    const parts = [...new Set(all.map(note => note.part))];
    const chosen = selection.events.filter(note => !options.part || options.part === 'all' || note.part === options.part)
      .sort((a, b) => a.start - b.start || a.pitch - b.pitch || String(a.id).localeCompare(String(b.id)));
    const targets = [];
    const lookup=new Map(all.map(note=>[note.id,note]));
    for (const event of chosen) {
      const note=lookup.get(event.noteIds[0]), start = event.start - origin, end = event.end - origin, pitch = event.pitch + transpose;
      targets.push({id: event.noteIds[0], noteIds: event.noteIds.slice(), performanceId: event.performanceId || null, eventId:event.id,
        pitch, pitchName: transpose === 0 && event.pitchName ? event.pitchName : describePitch(midiToHz(pitch, a4), a4).name,
        sourcePitch: event.pitch, sourcePitchName: event.pitchName || null, frequency: midiToHz(pitch, a4), start, end, part: event.part,
        measure: note?.measure, selectionClipped:Boolean(event.selectionClipped)});
    }
    const conflicts = [];
    for (let i = 0; i < targets.length; i++) {
      for (let j = i + 1; j < targets.length && targets[j].start < targets[i].end - 1e-6; j++) {
        if (targets[i].pitch !== targets[j].pitch) conflicts.push({start: Math.max(targets[i].start, targets[j].start),
          end: Math.min(targets[i].end, targets[j].end), ids: [targets[i].id, targets[j].id]});
      }
    }
    return {targets, origin, duration, parts, part: options.part || null, transpose, a4,eventSource:selection.eventSource,
      conflicts, monophonic: conflicts.length === 0};
  }

  function targetAtTime(timeline, time, options = {}) {
    const targets = Array.isArray(timeline) ? timeline : timeline && timeline.targets || [];
    if (options.mode === 'fixed') {
      return targets.find(target => target.id === options.targetId || target.noteIds.includes(options.targetId)) || targets[0] || null;
    }
    if (!Number.isFinite(time)) return null;
    const active = targets.filter(target => target.start <= time && time < target.end);
    if (!active.length || active.some(target => target.pitch !== active[0].pitch)) return null;
    return active[0];
  }

  function median(values) {
    if (!values.length) return null;
    const sorted = [...values].sort((a, b) => a - b), middle = Math.floor(sorted.length / 2);
    return sorted.length % 2 ? sorted[middle] : (sorted[middle - 1] + sorted[middle]) / 2;
  }
  function summarizeFrames(frames, options = {}) {
    const tolerance = Math.max(0, finite(options.toleranceCents, 25));
    const voiced = frames.filter(frame => frame.frequency > 0);
    const expected = frames.filter(frame => frame.targetFrequency > 0);
    const matched = expected.filter(frame => Number.isFinite(frame.cents));
    const values = matched.map(frame => frame.cents);
    const perTarget = {};
    for (const frame of expected) {
      const stats = perTarget[frame.targetId] || (perTarget[frame.targetId] = {targetId: frame.targetId, frequency: frame.targetFrequency, expectedFrames: 0, matchedFrames: 0, cents: []});
      stats.expectedFrames++;
      if (Number.isFinite(frame.cents)) {stats.matchedFrames++; stats.cents.push(frame.cents);}
    }
    for (const stats of Object.values(perTarget)) {
      stats.medianCents = median(stats.cents);
      stats.medianAbsoluteCents = median(stats.cents.map(Math.abs));
      stats.coverage = stats.matchedFrames / stats.expectedFrames;
      delete stats.cents;
    }
    return {totalFrames: frames.length, voicedFrames: voiced.length, expectedFrames: expected.length,
      matchedFrames: matched.length, coverage: expected.length ? matched.length / expected.length : 0,
      medianCents: median(values), medianAbsoluteCents: median(values.map(Math.abs)),
      meanAbsoluteCents: values.length ? values.reduce((sum, value) => sum + Math.abs(value), 0) / values.length : null,
      inTuneFraction: values.length ? values.filter(value => Math.abs(value) <= tolerance).length / values.length : null,
      toleranceCents: tolerance, perTarget};
  }

  function analyzeRecording(input, sampleRate, options = {}) {
    if (!input || !Number.isFinite(sampleRate) || sampleRate <= 0) throw new Error('Invalid PCM or sample rate');
    const channels = Array.isArray(input) && input.length && typeof input[0] !== 'number' ? input : [input];
    const length = Math.min(...channels.map(channel => channel.length));
    const duration = length / sampleRate;
    if (duration > finite(options.maxDuration, 300)) throw new Error('Recording exceeds analysis duration limit');
    const mono = new Float32Array(length);
    for (const channel of channels) for (let i = 0; i < length; i++) mono[i] += channel[i] / channels.length;
    const windowSize = Math.max(256, Math.ceil(sampleRate * Math.max(0.04, finite(options.windowSeconds, 0.096))));
    const hop = Math.max(1, Math.round(sampleRate * Math.max(0.01, finite(options.hopSeconds, 0.04))));
    const offset = finite(options.offset, 0), rate = Math.max(0.1, finite(options.playbackRate, 1));
    const frames = [];
    const timeline = options.targets || [];
    for (let start = 0; start + windowSize <= length; start += hop) {
      const time = (start + windowSize / 2) / sampleRate;
      const referenceTime = (time - offset) * rate;
      const detected = detectPitch(mono.subarray(start, start + windowSize), sampleRate, {...options, maxSamples: windowSize});
      const target = targetAtTime(timeline, referenceTime, options);
      frames.push({...detected, time, referenceTime, targetId: target ? target.id : null,
        targetFrequency: target ? target.frequency : null,
        cents: target && detected.frequency ? centsDifference(detected.frequency, target.frequency) : null,
        hzDifference: target && detected.frequency ? detected.frequency - target.frequency : null});
    }
    return {frames, summary: summarizeFrames(frames, options), duration, sampleRate,
      options: {offset, playbackRate: rate, mode: options.mode || 'follow', targetId: options.targetId || null,
        windowSeconds: windowSize / sampleRate, hopSeconds: hop / sampleRate}};
  }


  // A playback reference is not a source separator. This bounded linear model is
  // learnt ONLY while the singer stays silent and then frozen during singing.
  // Browser AEC remains the first line of defence; ambiguous leakage is rejected.
  function practiceRegion(input, selectedIds, options = {}) {
    const selection=performanceSelection(input,{...options,noteIds:selectedIds || []}), selected=selection.notes;
    const origin = selected.length ? Math.min(...selected.map(n => n.start)) : 0;
    const end = selected.length ? Math.max(...selected.map(n => n.end)) : origin;
    const backgroundParts = [...new Set(options.backgroundParts || [])].filter(part => part !== options.part);
    const all=performanceSelection(input,{...options,noteIds:null});
    const background=all.events.filter(event=>backgroundParts.includes(event.part) && event.start<end && event.end>origin)
      .map(event=>({...event,start:Math.max(origin,event.start),end:Math.min(end,event.end),performanceId:event.performanceId || event.id}));
    const backgroundNotes=all.allNotes.filter(note=>backgroundParts.includes(note.part) && note.start<end && note.end>origin);
    const timeline = buildTargets(input,{...options,noteIds:selectedIds || []});
    // All selected parts set the common region origin, preserving leading rests
    // in the singer's part and clipping only its selected notation members.
    timeline.origin = origin; timeline.duration = end - origin;
    return {timeline, origin, end, duration:end-origin, backgroundParts, backgroundIds:backgroundNotes.map(n=>n.id), background};
  }

  function referenceValidation(model, selectedIds, options = {}) {
    const policy=options.rhythmPolicy || 'strict', validation=model?.rhythmValidation;
    if (!['strict','draft'].includes(policy)) throw new Error('rhythmPolicy 必须为 strict 或 draft');
    if (!validation) {
      return Number(model?.schemaVersion || 1)>=2 ? {allowed:false,referenceStatus:'unvalidated',blockingMeasures:[],scopeMeasureIndices:[],message:'目标乐谱缺少节奏校验，请重新生成曲谱。'} : {allowed:true,referenceStatus:'legacy',blockingMeasures:[],scopeMeasureIndices:[],message:''};
    }
    const notes=model.notes || [], chosen=selectedIds==null?notes:notes.filter(note=>new Set(selectedIds).has(note.id));
    const origin=chosen.length?Math.min(...chosen.map(note=>note.start)):0, end=chosen.length?Math.max(...chosen.map(note=>note.end)):origin;
    const scope=(model.measures || []).filter(measure=>selectedIds==null || (measure.start<end && measure.end>origin)).map(measure=>measure.index);
    const blocking=new Set(validation.blockingMeasureIndices || []), indices=scope.filter(index=>blocking.has(index));
    const labels=new Map((model.measures || []).map(measure=>[measure.index,measure.number])), blockingMeasures=indices.map(index=>labels.get(index) ?? index);
    const unlocated=validation.status!=='validated' && !blocking.size, unresolved=Boolean(indices.length || unlocated);
    return {allowed:policy==='draft' || !unresolved,referenceStatus:unresolved?'draft':'rhythm-validated',blockingMeasures,scopeMeasureIndices:scope,
      message:unresolved?`目标小节${blockingMeasures.length?'（'+blockingMeasures.slice(0,12).join('、')+'）':''}待校对，可先修订或开启播放面板草稿试听。`:''};
  }
  function referenceStats(samples, reference, delay, step = 1) {
    const first = reference.length - samples.length - delay;
    let meanMic=0, meanRef=0, count=0;
    for(let i=0;i<samples.length;i+=step){meanMic+=samples[i];meanRef+=reference[first+i] || 0;count++;}
    meanMic/=count;meanRef/=count;
    let dot=0,micEnergy=0,refEnergy=0;
    for(let i=0;i<samples.length;i+=step){const x=samples[i]-meanMic,r=(reference[first+i] || 0)-meanRef;dot+=x*r;micEnergy+=x*x;refEnergy+=r*r;}
    return {dot,micEnergy,refEnergy,count,meanMic,meanRef,correlation:dot/Math.sqrt(Math.max(1e-20,micEnergy*refEnergy)),gain:dot/Math.max(1e-20,refEnergy)};
  }
  function calibrateReference(frames, sampleRate, options = {}) {
    const available=(frames || []).filter(f=>f.samples?.length>=256 && f.reference?.length>=f.samples.length);
    if(available.length<3)return {valid:false,reason:'insufficient-calibration',delaySamples:0,gain:0};
    const maxDelay = Math.max(0,Math.min(...available.map(f=>f.reference.length-f.samples.length),Math.round(sampleRate*finite(options.maxDelaySeconds,0.25))));
    const coarse=Math.max(1,Math.round(sampleRate*0.00025)),step=Math.max(1,Math.floor(sampleRate/6000));
    const aggregate=delay=>{
      let dot=0,micEnergy=0,refEnergy=0,count=0;
      for(const frame of available){const s=referenceStats(frame.samples,frame.reference,delay,step);dot+=s.dot;micEnergy+=s.micEnergy;refEnergy+=s.refEnergy;count+=s.count;}
      const correlation=dot/Math.sqrt(Math.max(1e-20,micEnergy*refEnergy));
      return {delaySamples:delay,gain:dot/Math.max(1e-20,refEnergy),correlation,micRms:Math.sqrt(micEnergy/count),referenceRms:Math.sqrt(refEnergy/count)};
    };
    let best=aggregate(0);
    for(let delay=coarse;delay<=maxDelay;delay+=coarse){const result=aggregate(delay);if(Math.abs(result.correlation)>Math.abs(best.correlation))best=result;}
    const middle=best.delaySamples;
    for(let delay=Math.max(0,middle-coarse);delay<=Math.min(maxDelay,middle+coarse);delay++){const result=aggregate(delay);if(Math.abs(result.correlation)>Math.abs(best.correlation))best=result;}
    let residualEnergy=0,count=0;
    for(const frame of available){const start=frame.reference.length-frame.samples.length-best.delaySamples;for(let i=0;i<frame.samples.length;i++){const delta=frame.samples[i]-best.gain*(frame.reference[start+i] || 0);residualEnergy+=delta*delta;count++;}}
    const noiseRms=Math.sqrt(residualEnergy/Math.max(1,count));
    const valid=best.micRms>=finite(options.minCalibrationRms,0.003) && best.referenceRms>=0.003 && Math.abs(best.correlation)>=0.72 && Math.abs(best.gain)<=4;
    return {...best,noiseRms,valid,delaySeconds:best.delaySamples/sampleRate,explainedFraction:best.correlation*best.correlation,frames:available.length,
      reason:valid?'calibrated-linear-reference':best.micRms<0.003?'aec-or-quiet-input':'unreliable-reference'};
  }
  function suppressReference(samples, reference, calibration, sampleRate, options = {}) {
    const input=Float32Array.from(samples),minRms=finite(options.minRms,0.008);
    const delay=calibration?.valid?calibration.delaySamples:0;
    const before=referenceStats(input,reference,delay),gain=calibration?.valid?calibration.gain:0;
    const start=reference.length-input.length-delay;
    let energy=0,rawEnergy=0;
    for(let i=0;i<input.length;i++){const raw=input[i];input[i]=raw-gain*(reference[start+i] || 0);energy+=input[i]*input[i];rawEnergy+=raw*raw;}
    const rms=Math.sqrt(energy/input.length),rawRms=Math.sqrt(rawEnergy/input.length),coherence=Math.abs(referenceStats(input,reference,delay).correlation);
    const leakageFloor=calibration?.valid?Math.max(minRms,calibration.noiseRms*2.5):minRms;
    const rejection=rms<leakageFloor?'reference-only':(rms>minRms && coherence>0.7 && before.refEnergy/before.count>1e-5)?'reference-dominated':null;
    return {samples:input,rms,rawRms,coherence,correlation:before.correlation,referenceGain:gain,referenceDelaySeconds:delay/sampleRate,
      reliable:!rejection,reason:rejection || (calibration?.valid?'calibrated-residual':'aec-only-residual')};
  }
  function assessInterference(samples, reference, calibration, sampleRate, options = {}) {
    const residual=suppressReference(samples,reference,calibration,sampleRate,options);
    const detection=residual.reliable?detectPitch(residual.samples,sampleRate,options):{frequency:null,confidence:0,rms:residual.rms,reason:residual.reason,voiced:false};
    // Same notes and shared harmonics are acoustically ambiguous. Reject rather
    // than selecting the nearest expected singer note and inventing a pass.
    const collision=detection.frequency && (options.backgroundFrequencies || []).some(f=>[0.5,1,2,3,4].some(multiplier=>Math.abs(centsDifference(detection.frequency,f*multiplier))<8));
    if(collision){detection.frequency=null;detection.voiced=false;detection.reason='background-unison';}
    return {...detection,signal:{rawRms:residual.rawRms,residualRms:residual.rms,referenceCoherence:residual.coherence,referenceGain:residual.referenceGain,referenceDelaySeconds:residual.referenceDelaySeconds,calibrated:Boolean(calibration?.valid),suppressed:!detection.frequency,reason:detection.reason},samples:residual.samples};
  }

  return {practiceRegion, referenceValidation, calibrateReference, suppressReference, assessInterference, midiToHz, frequencyForMidi: midiToHz, centsDifference, describePitch,
    detectPitch, estimate: detectPitch, fftSpectrum, spectrum: fftSpectrum, buildTargets,
    targetAtTime, analyzeRecording, summarizeFrames};
});
