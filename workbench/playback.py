"""Create MIDI directly from canonical events; selection keeps polyphonic timing."""
from pathlib import Path
import math
import mido
GROUPS=('S','A','T','B','Piano')
PROGRAMS=(0,48,52,53,91)

def validate_settings(settings):
    transpose=settings.get('transpose',0);program=settings.get('program',52);gains=settings.get('gains',{})
    if type(transpose) is not int or not -12<=transpose<=12:raise ValueError('移调必须为 −12 至 +12 半音')
    if type(program) is not int or program not in PROGRAMS:raise ValueError('音色无效')
    if not isinstance(gains,dict) or set(gains)-set(GROUPS):raise ValueError('声部无效')
    for value in gains.values():
        if type(value) not in (int,float) or not math.isfinite(value) or not 0<=value<=1.5:raise ValueError('声部音量必须在 0–150% 之间')
    a4=settings.get('a4',440)
    if type(a4) not in (int,float) or not math.isfinite(a4) or not 400<=a4<=480:raise ValueError('A4 参考频率必须在 400–480 Hz 之间')
    rate=settings.get('practiceRate',1)
    if type(rate) not in (int,float) or not math.isfinite(rate) or not .25<=rate<=2:raise ValueError('练习速度倍率必须在 0.25–2 之间')
    return transpose,program,gains

def _selected_notes(model, note_ids=None):
    notes=model['notes']
    if note_ids is None:return notes
    if not isinstance(note_ids,list) or not 1<=len(note_ids)<=4000 or any(not isinstance(x,str) for x in note_ids):raise ValueError('请选择 1–4000 个音符')
    lookup={n['id']:n for n in notes}
    if any(x not in lookup for x in note_ids):raise ValueError('选区包含失效音符，请刷新曲谱')
    return [lookup[x] for x in dict.fromkeys(note_ids)]


def _legacy_performances(notes):
    """Compatibility for old note-only fixtures; canonical models never use this."""
    events=[];chains={}
    for note in sorted(notes,key=lambda n:(n['start'],n['id'])):
        if note.get('grace') or not isinstance(note.get('pitch'),(int,float)) or note['end']<=note['start']:continue
        key=(note.get('sourcePart',note.get('part')),note.get('staff',1),note.get('voice',1),note['pitch'])
        tie=note.get('tie');types=set(tie if isinstance(tie,list) else [tie])
        previous=chains.get(key)
        if ('stop' in types or 'continue' in types) and previous is not None and abs(previous['end']-note['start'])<.001:
            previous['end']=note['end'];previous['noteIds'].append(note['id']);event=previous
        else:
            event={**note,'id':f"p-{note['id']}",'noteIds':[note['id']]};events.append(event)
        if 'start' in types or 'continue' in types:chains[key]=event
        else:chains.pop(key,None)
    return events


def _performance_events(model):
    events=model.get('performanceEvents')
    if events is None:
        if model.get('schemaVersion',1)>=2:raise ValueError('规范乐谱缺少演奏事件，请重新生成曲谱')
        return _legacy_performances(model['notes'])
    if not isinstance(events,list):raise ValueError('演奏事件格式无效，请重新生成曲谱')
    lookup={note['id']:note for note in model['notes']};members={};event_ids=set()
    for event in events:
        identifiers=event.get('noteIds')
        if not isinstance(event.get('id'),str) or event['id'] in event_ids:raise ValueError('演奏事件 ID 无效或重复')
        event_ids.add(event['id'])
        if not isinstance(identifiers,list) or not identifiers or len(set(identifiers))!=len(identifiers):raise ValueError('演奏事件缺少有效音符成员')
        if any(type(event.get(key)) not in (int,float) or not math.isfinite(event[key]) for key in ('start','end','pitch')) or event['end']<=event['start']:raise ValueError('演奏事件的音高或时间无效')
        for identifier in identifiers:
            note=lookup.get(identifier)
            if note is None or identifier in members:raise ValueError('演奏事件音符成员失效或重复')
            if note.get('performanceId') not in (None,event['id']) or note.get('part')!=event.get('part') or note.get('pitch')!=event['pitch']:raise ValueError('音符与演奏事件不一致，请重新生成曲谱')
            if note['start']<event['start']-1e-6 or note['end']>event['end']+1e-6:raise ValueError('音符超出演奏事件时间范围')
            members[identifier]=event['id']
    if model.get('schemaVersion',1)>=2:
        missing=[note['id'] for note in model['notes'] if note.get('kind','note')!='rest' and not note.get('grace') and note.get('pitch') is not None and note['end']>note['start'] and note['id'] not in members]
        if missing:raise ValueError('规范音符缺少演奏事件，请重新生成曲谱')
    return events


def selected_events(model,note_ids=None):
    """Clip canonical events to selected notation members, without re-reading ties.

    Selecting a continuation alone auditions that written segment. A whole tie
    chain retains one canonical attack; separated selected members remain
    separate clips, so unselected middle symbols are not silently sounded.
    """
    selected=_selected_notes(model,note_ids)
    events=_performance_events(model)
    if note_ids is None:return [{**event,'performanceId':event['id']} for event in events]
    selected_ids={note['id'] for note in selected};lookup={note['id']:note for note in selected};result=[]
    for event in events:
        members=[identifier for identifier in event['noteIds'] if identifier in selected_ids]
        if not members:continue
        if len(members)==len(event['noteIds']):
            result.append({**event,'performanceId':event['id']});continue
        clips=[]
        for identifier in sorted(members,key=lambda identifier:(lookup[identifier]['start'],identifier)):
            note=lookup[identifier];start=max(event['start'],note['start']);end=min(event['end'],note['end'])
            if end<=start:continue
            if clips and start<=clips[-1]['end']+1e-6:
                clips[-1]['end']=max(clips[-1]['end'],end);clips[-1]['noteIds'].append(identifier)
            else:clips.append({'start':start,'end':end,'noteIds':[identifier]})
        for index,clip in enumerate(clips):
            result.append({**event,**clip,'id':f"{event['id']}::clip{index+1}",'performanceId':event['id'],'selectionClipped':True})
    return sorted(result,key=lambda event:(event['start'],event['id']))

def make_midi(model,destination,settings=None,note_ids=None):
    settings=settings or {}
    transpose,program,gains=validate_settings(settings)
    a4=settings.get('a4',440)
    rate=settings.get('practiceRate',1)
    # Explicit RPN pitch-bend range of two semitones, then a constant tuning
    # bend. The configured A4 applies equally to synthesis and pitch targets.
    tuning_bend=round(8192*12*math.log2(a4/440)/2)
    selected=selected_events(model,note_ids)
    if not selected:raise ValueError('选区没有可播放的音符')
    notation=_selected_notes(model,note_ids)
    start=min(n['start'] for n in notation) if note_ids is not None else 0
    end=max(n['end'] for n in notation) if note_ids is not None else model['duration']
    # Second-based canonical timing converted to fixed 120 BPM MIDI, preserving
    # all tempo changes already represented in the canonical start/end seconds.
    midi=mido.MidiFile(ticks_per_beat=960)
    meta=mido.MidiTrack();meta.append(mido.MetaMessage('set_tempo',tempo=500000));midi.tracks.append(meta)
    channels=[c for c in range(16) if c!=9]
    by_group={g:[] for g in GROUPS}
    unisons={'strategy':'reference-count-same-group-pitch','semanticEvents':len(selected),'serializedAttacks':0,'sameTickMergedAttacks':0,'laterSuppressedAttacks':0,'byGroup':{}}
    for n in selected:
        group=n.get('part',n.get('group','Piano'))
        if group not in GROUPS:group=n.get('group','Piano')
        by_group[group].append(n)
    for group,notes in by_group.items():
        if not notes:continue
        channel=channels[GROUPS.index(group)];track=mido.MidiTrack();midi.tracks.append(track)
        track.append(mido.MetaMessage('track_name',name=group))
        track.append(mido.Message('program_change',channel=channel,program=0 if group=='Piano' else program))
        if tuning_bend:
            for control,value in ((101,0),(100,0),(6,2),(38,0),(101,127),(100,127)):
                track.append(mido.Message('control_change',channel=channel,control=control,value=value))
            track.append(mido.Message('pitchwheel',channel=channel,pitch=tuning_bend))
        events=[]
        for n in notes:
            pitch=n['pitch']+transpose
            if not 0<=pitch<=127:raise ValueError('移调后超出 MIDI 音高范围')
            # Reschedule the synthesis without transposing the pitches. This is
            # also the reference clock for speaker-backed singing practice.
            a=round((n['start']-start)*1920/rate);b=round((n['end']-start)*1920/rate)
            velocity=min(127,max(0,round(80*gains.get(group,1))))
            events.extend([(a,1,pitch,velocity),(max(a+1,b),0,pitch,0)])
        # Reference-count simultaneous unisons on a MIDI channel, so one note's
        # release never cuts another voice's same-pitch note short.
        active={};attack_tick={};last=0;stats={'semanticEvents':len(notes),'serializedAttacks':0,'sameTickMergedAttacks':0,'laterSuppressedAttacks':0}
        for tick,on,pitch,velocity in sorted(events):
            count=active.get(pitch,0)
            active[pitch]=count+1 if on else max(0,count-1)
            if on and count:
                key='sameTickMergedAttacks' if attack_tick.get(pitch)==tick else 'laterSuppressedAttacks'
                stats[key]+=1;continue
            if not on and active[pitch]:continue
            if on:attack_tick[pitch]=tick;stats['serializedAttacks']+=1
            else:attack_tick.pop(pitch,None)
            track.append(mido.Message('note_on' if on else 'note_off',channel=channel,note=pitch,velocity=velocity,time=tick-last));last=tick
        unisons['byGroup'][group]=stats
        for key in ('serializedAttacks','sameTickMergedAttacks','laterSuppressedAttacks'):unisons[key]+=stats[key]
        track.append(mido.MetaMessage('end_of_track',time=max(0,round((end-start)*1920/rate)-last)))
    midi.save(str(destination))
    return {'start':start,'duration':(end-start)/rate,'scoreDuration':end-start,'practiceRate':rate,
            'noteIds':[n['id'] for n in notation],'performanceIds':list(dict.fromkeys(n['performanceId'] for n in selected)),
            'eventSource':'performanceEvents' if 'performanceEvents' in model else 'legacy-notes','a4':a4,'unisonRendering':unisons}


def fit_audio_duration(path, duration):
    """Keep selected rests and a defined tail; avoid FluidSynth's extra seconds."""
    import shutil, subprocess
    ffmpeg=shutil.which('ffmpeg')
    if not ffmpeg:raise RuntimeError('未找到 ffmpeg')
    source=Path(path);target=source.with_name(source.stem+'.duration.wav')
    subprocess.run([ffmpeg,'-y','-i',str(source),'-af',f'apad=whole_dur={duration:.6f},atrim=duration={duration:.6f}',
                    '-c:a','pcm_s16le',str(target)],check=True,capture_output=True)
    target.replace(source)
