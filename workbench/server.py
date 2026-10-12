"""Loopback-only score workbench. Run with env/venv/bin/python workbench/server.py."""
from pathlib import Path
import sys, json, mimetypes, argparse, threading, uuid
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlsplit, unquote
import mido
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from synth.render import midi_to_wav, wav_to_mp3, _trim_trailing_silence
WEB=ROOT/'workbench'; LOCK=threading.Lock()
ASSETS=None; GENERATED=None; PREVIEW=None

from workbench.playback import make_midi, selected_events, fit_audio_duration
from workbench.validation import require_rhythm

def read_model():
    if PREVIEW is None:raise ValueError("An explicit bundle/config preview is required")
    return PREVIEW.read_model()

from workbench.calibration_service import CalibrationManager
CALIBRATION=None

def configure_preview(bundle, config, generated=None):
    """Bind this server process to one isolated project; leave default assets alone."""
    global PREVIEW, GENERATED, CALIBRATION
    from workbench.preview import BundlePreview
    preview=BundlePreview(bundle,config,generated)
    PREVIEW=preview;GENERATED=preview.generated
    CALIBRATION=CalibrationManager(read_model,LOCK,read_config=preview.read_config,
        generated=GENERATED,receipt_path=preview.receipt,publish=preview.publish)
    return preview

UI_FILES={'index.html','entry.js','entry.css','calibration_ui.js','calibration_ui.css','app.js','style.css',
          'interactive.js','interactive.css','console.js','console.css','metronome.js','pitch_core.js',
          'pitch.js','pitch.css','pitch-worklet.js','pitch-worker.js','pitch-reference-worklet.js'}

def resolve_static(path):
    """Resolve media in this project only, while sharing the application code."""
    if path.lstrip('/') in UI_FILES:
        return WEB/path.lstrip('/')
    for prefix,root in (('/assets/',PREVIEW.assets if PREVIEW else ASSETS),('/generated/',GENERATED)):
        if path.startswith(prefix):
            file=(root/path[len(prefix):]).resolve()
            if file.is_relative_to(root.resolve()):return file
    return None

def make_mix(settings, selection=False):
    model=read_model()
    note_ids=settings.get('noteIds') if selection else None
    if selection and note_ids is None:raise ValueError('请先选择音符')
    validation=require_rhythm(model,settings.get('rhythmPolicy','strict'),note_ids)
    ident=uuid.uuid4().hex[:12];target=GENERATED/ident;target.mkdir(parents=True)
    info=make_midi(model,target/'mix.mid',settings,note_ids)
    soundfont=PREVIEW.config['soundfont']
    wav=midi_to_wav(str(target/'mix.mid'),soundfont,str(target/'mix.wav'))
    # Preserve silence within the canonical region; bound synth release to 0.35s.
    fit_audio_duration(wav,info['duration']+(0.35 if selection else 0))
    wav_to_mp3(wav,str(target/'mix.mp3'))
    return dict(info,audio=f'/generated/{ident}/mix.mp3',wav=f'/generated/{ident}/mix.wav',
                midi=f'/generated/{ident}/mix.mid',revision=model['revision'],selection=selection,audioValidation=validation)

def correct_note(data):
    if PREVIEW is None:raise ValueError("An explicit bundle/config preview is required")
    return PREVIEW.correct_note(data)

class Handler(BaseHTTPRequestHandler):
    def json_response(self,status,data):
        body=json.dumps(data,ensure_ascii=False).encode();self.send_response(status);self.send_header('Content-Type','application/json; charset=utf-8');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
    def do_POST(self):
        if self.path not in ('/api/render','/api/selection','/api/correction','/api/calibration'):return self.json_response(404,{'error':'不存在的接口'})
        origin=self.headers.get('Origin')
        if origin and origin!=f'http://127.0.0.1:{self.server.server_port}':return self.json_response(403,{'error':'仅接受本地工作台请求'})
        if self.headers.get('Content-Type')!='application/json':return self.json_response(415,{'error':'需要 JSON 参数'})
        if self.path=='/api/calibration':
            try:
                size=int(self.headers.get('Content-Length','0'))
                if not 0<size<=300000:raise ValueError('请求大小无效')
                data=json.loads(self.rfile.read(size))
                if not isinstance(data,dict):raise ValueError('参数必须为对象')
                return self.json_response(202,CALIBRATION.start(data))
            except (ValueError,json.JSONDecodeError) as e:return self.json_response(400,{'error':str(e)})
            except RuntimeError as e:return self.json_response(409,{'error':str(e)})
        if not LOCK.acquire(blocking=False):return self.json_response(409,{'error':'正在生成，请稍后再试'})
        try:
            size=int(self.headers.get('Content-Length','0'))
            if not 0<size<=300000:raise ValueError('请求大小无效')
            data=json.loads(self.rfile.read(size))
            if not isinstance(data,dict):raise ValueError('参数必须为对象')
            result=correct_note(data) if self.path=='/api/correction' else make_mix(data,self.path=='/api/selection')
            self.json_response(200,result)
        except (ValueError,json.JSONDecodeError) as e:self.json_response(400,{'error':str(e)})
        except Exception as e:self.json_response(500,{'error':f'生成失败：{e}'})
        finally:LOCK.release()
    def do_GET(self):
        path=unquote(urlsplit(self.path).path)
        if path=='/api/health':return self.json_response(200,{'service':'sheet-to-audio-workbench','workspace':str(ROOT),'revision':read_model()['revision'],
            'bundle':str(PREVIEW.bundle) if PREVIEW else None,'baseBundle':str(PREVIEW.base_bundle) if PREVIEW else None,
            'config':str(PREVIEW.config_path) if PREVIEW else None})
        if path=='/api/calibration':
            try:return self.json_response(200,CALIBRATION.snapshot())
            except (ValueError,OSError,KeyError) as e:return self.json_response(500,{'error':f'校准记录读取失败：{e}'})
        if path=='/':path='/index.html'
        if path=='/assets/score.json' and PREVIEW:return self.json_response(200,PREVIEW.score_projection())
        file=resolve_static(path)
        if file is None or not file.is_file():return self.send_error(404)
        size=file.stat().st_size;start=0;end=size-1
        header=self.headers.get('Range')
        try:
            if header:
                unit,span=header.split('=',1)
                if unit!='bytes' or ',' in span:raise ValueError()
                first,last=span.split('-',1)
                if first:start=int(first);end=min(int(last),size-1) if last else size-1
                else:start=max(0,size-int(last))
                if start<0 or start>end:raise ValueError()
        except ValueError:
            self.send_response(416);self.send_header('Content-Range',f'bytes */{size}');self.end_headers();return
        self.send_response(206 if header else 200)
        self.send_header('Content-Type',mimetypes.guess_type(file)[0] or 'application/octet-stream')
        self.send_header('Content-Length',str(end-start+1));self.send_header('Accept-Ranges','bytes')
        if header:self.send_header('Content-Range',f'bytes {start}-{end}/{size}')
        self.end_headers()
        try:
            with file.open('rb') as f:
                f.seek(start); remaining=end-start+1
                while remaining:
                    chunk=f.read(min(65536,remaining));self.wfile.write(chunk);remaining-=len(chunk)
        except (BrokenPipeError,ConnectionResetError):pass

def main():
    p=argparse.ArgumentParser();p.add_argument('--port',type=int,default=8765)
    p.add_argument('--bundle',required=True,help='已验证的独立 workflow 输出目录')
    p.add_argument('--config',required=True,help='独立曲谱项目配置，与 --bundle 同时使用')
    p.add_argument('--generated',help='该预览的独立音频和校准输出目录')
    args=p.parse_args()
    if bool(args.bundle)!=bool(args.config):p.error('--bundle 与 --config 需要同时提供')
    if args.generated and not args.bundle:p.error('--generated 仅用于独立预览')
    if not 1<=args.port<=65535:p.error('port 应在1–65535')
    configure_preview(args.bundle,args.config,args.generated)
    server=ThreadingHTTPServer(('127.0.0.1',args.port),Handler)
    print(f'工作台：http://127.0.0.1:{args.port}',flush=True)
    try:server.serve_forever()
    except KeyboardInterrupt:server.server_close()
if __name__=='__main__':main()
