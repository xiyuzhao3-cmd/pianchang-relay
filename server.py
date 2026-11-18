#!/usr/bin/env python3
"""片场 Muse relay: Python 3.11+, SQLite, no third-party Python dependencies."""
import os, json, sqlite3, secrets, hashlib, hmac, time, threading, re, subprocess, tempfile, shutil
from pathlib import Path
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, quote
from urllib.request import Request, urlopen
from email.parser import BytesParser
from email.policy import default

ROOT=Path(os.getenv('DATA_DIR','./data')).resolve(); ROOT.mkdir(parents=True,exist_ok=True)
FILES=ROOT/'files';FILES.mkdir(exist_ok=True)
CLIENT=os.getenv('CLIENT_TOKEN','');MUSE=os.getenv('MUSE_TOKEN','');SIGN=os.getenv('SIGNING_KEY','')
ORIGIN=os.getenv('ALLOWED_ORIGIN','https://pianchang-education-demo.rtt0.chatgpt.site').rstrip('/')
PUBLIC=os.getenv('PUBLIC_BASE_URL','http://127.0.0.1:8787').rstrip('/')
HOSTS=set(filter(None,os.getenv('RESULT_HOST_ALLOWLIST','').split(',')))
MAX_UPLOAD=50*1024*1024; MAX_RESULT=150*1024*1024; LEASE=1800

def db():
    c=sqlite3.connect(ROOT/'tasks.sqlite',timeout=15);c.row_factory=sqlite3.Row
    c.execute('PRAGMA journal_mode=WAL');return c

def init():
    with db() as c:
        c.execute('CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY,payload TEXT NOT NULL,status TEXT NOT NULL,created REAL,updated REAL,lease REAL,claim TEXT,result TEXT,progress TEXT,idem TEXT UNIQUE)')
        c.execute('CREATE INDEX IF NOT EXISTS task_status ON tasks(status,created)')

def public_task(row):
    if not row:return None
    x=json.loads(row['payload']);x.update(id=row['id'],status=row['status'],progress=row['progress'],created=row['created'])
    if row['result']:x.update(json.loads(row['result']))
    return x

def signed_url(name):
    expiry=int(time.time()+7*86400);message=f'{name}:{expiry}'.encode();signature=hmac.new(SIGN.encode(),message,hashlib.sha256).hexdigest()
    return f'{PUBLIC}/files/{quote(name)}?expires={expiry}&signature={signature}'

def valid_url(value):
    if not isinstance(value,str):return False
    u=urlparse(value)
    return u.scheme=='https' and bool(u.hostname) and not u.username and not u.password and u.hostname not in ('localhost','127.0.0.1','::1')

def safe_local(url):
    u=urlparse(url);base=urlparse(PUBLIC)
    if u.netloc==base.netloc and re.fullmatch(r'/files/[a-f0-9]{32}\.[a-z0-9]{1,5}',u.path):
        file=FILES/u.path.rsplit('/',1)[-1]
        if file.is_file():return file
    return None

def fetch_media(url,dest):
    local=safe_local(url)
    if local:shutil.copyfile(local,dest);return
    if not valid_url(url) or urlparse(url).hostname not in HOSTS:raise ValueError('外部视频合成需要在 RESULT_HOST_ALLOWLIST 允许结果域名，或先上传至本后端')
    # No redirects: each source hostname must be explicitly allowed.
    from urllib.request import HTTPRedirectHandler, build_opener
    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self,*args,**kwargs):return None
    with build_opener(NoRedirect()).open(Request(url,headers={'User-Agent':'Pianchang-Relay/1'}),timeout=45) as response,open(dest,'wb') as out:
        size=0
        while True:
            chunk=response.read(1024*1024)
            if not chunk:break
            size+=len(chunk)
            if size>MAX_RESULT:raise ValueError('单个结果文件超过150MB')
            out.write(chunk)

def compose_task(tid,payload):
    try:
        with db() as c:c.execute("UPDATE tasks SET status='processing',updated=? WHERE id=?",(time.time(),tid))
        if not shutil.which('ffmpeg'):raise ValueError('服务器未安装 FFmpeg')
        with tempfile.TemporaryDirectory() as folder:
            folder=Path(folder);names=[]
            for i,url in enumerate(payload['files']):
                raw=folder/f'raw{i}.mp4';normalized=folder/f'part{i}.mp4';fetch_media(url,raw)
                # Normalize codec, dimensions and fps. Audio is required on source clips for this basic concat path.
                cmd=['ffmpeg','-y','-v','error','-i',str(raw),'-vf','scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2,fps=24','-c:v','libx264','-preset','fast','-c:a','aac','-ar','48000','-ac','2',str(normalized)]
                subprocess.run(cmd,check=True,timeout=180,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE);names.append(normalized)
            listing=folder/'list.txt';listing.write_text(''.join(f"file '{x}'\n" for x in names))
            name=secrets.token_hex(16)+'.mp4';output=FILES/name
            subprocess.run(['ffmpeg','-y','-v','error','-f','concat','-safe','0','-i',str(listing),'-c','copy','-movflags','+faststart',str(output)],check=True,timeout=180,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
        result={'file_url':signed_url(name),'message':'本集合成完成'}
        with db() as c:c.execute("UPDATE tasks SET status='done',result=?,updated=? WHERE id=?",(json.dumps(result),time.time(),tid))
    except Exception as error:
        message=str(error) if isinstance(error,ValueError) else 'FFmpeg合成失败，请检查片段编码/音轨与服务器日志'
        with db() as c:c.execute("UPDATE tasks SET status='failed',result=?,updated=? WHERE id=?",(json.dumps({'message':message}),time.time(),tid))

class Handler(BaseHTTPRequestHandler):
    server_version='PianchangRelay/1.0'
    def log_message(self,format,*args):pass # Do not log signed URLs or tokens.
    def send(self,body,status=200,headers=None):
        data=json.dumps(body,ensure_ascii=False).encode();self.send_response(status)
        self.send_header('Content-Type','application/json; charset=utf-8');self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        origin=self.headers.get('Origin')
        if origin==ORIGIN:self.send_header('Access-Control-Allow-Origin',ORIGIN);self.send_header('Vary','Origin')
        for k,v in (headers or {}).items():self.send_header(k,v)
        self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
    def do_OPTIONS(self):
        if self.headers.get('Origin')!=ORIGIN:return self.send({'error':'Origin not allowed'},403)
        self.send({},204,{'Access-Control-Allow-Methods':'GET,POST,OPTIONS','Access-Control-Allow-Headers':'Authorization,Content-Type,Idempotency-Key','Access-Control-Max-Age':'600'})
    def auth(self,kind):
        expected=MUSE if kind=='muse' else CLIENT
        token=self.headers.get('Authorization','')
        return bool(expected) and hmac.compare_digest(token,'Bearer '+expected)
    def read(self,limit=2*1024*1024):
        length=int(self.headers.get('Content-Length','0'))
        if length<1 or length>limit:raise ValueError('请求大小超限或缺少Content-Length')
        return self.rfile.read(length)
    def do_GET(self):
        try:self.get()
        except (ValueError,KeyError,TypeError) as e:self.send({'error':str(e)},400)
        except Exception:self.send({'error':'后端内部错误'},500)
    def get(self):
        u=urlparse(self.path);path=u.path
        if path.startswith('/files/'):return self.file(u)
        if path=='/health':
            if not (self.auth('client') or self.auth('muse')):return self.send({'error':'Unauthorized'},401)
            return self.send({'ok':True,'service':'Pianchang Muse Relay','ffmpeg':bool(shutil.which('ffmpeg'))})
        if path=='/api/tasks/next':
            if not self.auth('muse'):return self.send({'error':'Unauthorized'},401)
            with db() as c:
                c.execute('BEGIN IMMEDIATE')
                c.execute("UPDATE tasks SET status='queued',claim=NULL WHERE status='processing' AND lease<? AND lease IS NOT NULL",(time.time(),))
                row=c.execute("SELECT * FROM tasks WHERE status='queued' ORDER BY created LIMIT 1").fetchone()
                if not row:return self.send({'task':None})
                claim=secrets.token_urlsafe(24);c.execute("UPDATE tasks SET status='processing',claim=?,lease=?,updated=? WHERE id=?",(claim,time.time()+LEASE,time.time(),row['id']))
                task=public_task(row);task['status']='processing';task['claimToken']=claim
            return self.send({'task':task})
        if path=='/api/tasks':
            if not self.auth('client'):return self.send({'error':'Unauthorized'},401)
            with db() as c:rows=c.execute('SELECT * FROM tasks ORDER BY created DESC LIMIT 200').fetchall()
            return self.send({'tasks':[public_task(x) for x in rows]})
        match=re.fullmatch(r'/api/tasks/([a-f0-9]{32})',path)
        if match:
            if not self.auth('client'):return self.send({'error':'Unauthorized'},401)
            with db() as c:row=c.execute('SELECT * FROM tasks WHERE id=?',(match[1],)).fetchone()
            return self.send(public_task(row) or {'error':'Not found'},200 if row else 404)
        self.send({'error':'Not found'},404)
    def file(self,u):
        name=u.path.split('/')[-1]
        if not re.fullmatch(r'[a-f0-9]{32}\.[a-z0-9]{1,5}',name):return self.send({'error':'Not found'},404)
        q=parse_qs(u.query);expiry=int(q.get('expires',['0'])[0]);sig=q.get('signature',[''])[0]
        expected=hmac.new(SIGN.encode(),f'{name}:{expiry}'.encode(),hashlib.sha256).hexdigest()
        if expiry<time.time() or not hmac.compare_digest(sig,expected):return self.send({'error':'Expired or invalid file URL'},403)
        file=FILES/name
        if not file.is_file():return self.send({'error':'Not found'},404)
        size=file.stat().st_size;start=0;end=size-1;code=200
        range_header=self.headers.get('Range','');m=re.fullmatch(r'bytes=(\d+)-(\d*)',range_header)
        if m:
            start=int(m[1]);end=min(int(m[2]) if m[2] else size-1,size-1);code=206
            if start>end:return self.send({'error':'Invalid range'},416)
        mime={'.mp4':'video/mp4','.png':'image/png','.jpg':'image/jpeg','.webp':'image/webp','.mp3':'audio/mpeg','.wav':'audio/wav'}.get(file.suffix,'application/octet-stream')
        self.send_response(code);self.send_header('Content-Type',mime);self.send_header('X-Content-Type-Options','nosniff');self.send_header('Accept-Ranges','bytes');self.send_header('Content-Length',str(end-start+1));self.send_header('Access-Control-Allow-Origin',ORIGIN)
        if code==206:self.send_header('Content-Range',f'bytes {start}-{end}/{size}')
        self.end_headers()
        with open(file,'rb') as f:
            f.seek(start);remaining=end-start+1
            while remaining:
                chunk=f.read(min(1024*1024,remaining))
                if not chunk:break
                self.wfile.write(chunk);remaining-=len(chunk)
    def do_POST(self):
        try:self.post()
        except (ValueError,KeyError,TypeError,json.JSONDecodeError) as e:self.send({'error':str(e)},400)
        except Exception:self.send({'error':'后端内部错误'},500)
    def post(self):
        path=urlparse(self.path).path
        if self.headers.get('Origin') and self.headers.get('Origin')!=ORIGIN:return self.send({'error':'Origin not allowed'},403)
        match=re.fullmatch(r'/api/tasks/([a-f0-9]{32})/(result|progress)',path)
        if match:
            if not self.auth('muse'):return self.send({'error':'Unauthorized'},401)
            data=json.loads(self.read());tid,action=match.groups()
            with db() as c:
                c.execute('BEGIN IMMEDIATE');row=c.execute('SELECT * FROM tasks WHERE id=?',(tid,)).fetchone()
                if not row:return self.send({'error':'Task not found'},404)
                if not row['claim'] or not hmac.compare_digest(str(data.get('claimToken','')),row['claim']):return self.send({'error':'Invalid claimToken'},409)
                if row['status'] in ('done','failed'):return self.send(public_task(row))
                if row['status']!='processing' or (row['lease'] or 0)<time.time():return self.send({'error':'Task lease expired'},409)
                if action=='progress':c.execute('UPDATE tasks SET progress=?,lease=?,updated=? WHERE id=?',(str(data.get('progress',''))[:500],time.time()+LEASE,time.time(),tid))
                else:
                    status=data.get('status')
                    if status not in ('done','failed'):raise ValueError('status must be done or failed')
                    url=data.get('file_url','')
                    if status=='done' and not (valid_url(url) or safe_local(url)):raise ValueError('file_url必须是HTTPS可下载结果链接')
                    result={'message':str(data.get('message',''))[:2000],'file_url':url,'file_size_mb':data.get('file_size_mb')}
                    c.execute('UPDATE tasks SET status=?,result=?,updated=? WHERE id=?',(status,json.dumps(result),time.time(),tid))
            return self.send({'ok':True,'id':tid})
        if not self.auth('client'):return self.send({'error':'Unauthorized'},401)
        if path=='/api/uploads':
            raw=self.read(MAX_UPLOAD);content_type=self.headers.get('Content-Type','')
            message=BytesParser(policy=default).parsebytes(('Content-Type: '+content_type+'\r\nMIME-Version: 1.0\r\n\r\n').encode()+raw)
            parts=[p for p in message.iter_parts() if p.get_filename()]
            if not parts:raise ValueError('缺少file字段')
            part=parts[0];ext=Path(part.get_filename()).suffix.lower()
            if ext not in ('.png','.jpg','.jpeg','.webp','.mp4','.mp3','.wav'):raise ValueError('不支持的文件格式')
            if ext=='.jpeg':ext='.jpg'
            name=secrets.token_hex(16)+ext;payload=part.get_payload(decode=True)
            if not payload or len(payload)>MAX_UPLOAD:raise ValueError('文件大小超限')
            (FILES/name).write_bytes(payload);return self.send({'url':signed_url(name),'name':name},201)
        data=json.loads(self.read())
        if path=='/api/tasks':
            if data.get('type') not in ('image','video'):raise ValueError('type仅支持image/video')
            if not isinstance(data.get('prompt'),str) or not 1<=len(data['prompt'])<=50000:raise ValueError('prompt为空或过长')
            refs=data.get('refs',[])
            if not isinstance(refs,list) or len(refs)>20 or not all(valid_url(u) or safe_local(u) for u in refs):raise ValueError('参考素材必须为HTTPS链接，最多20项')
            if not isinstance(data.get('nodeId'),str) or len(data['nodeId'])>200:raise ValueError('缺少nodeId')
            payload={k:data[k] for k in ['type','nodeId','title','prompt','refs','output','model','projectId','episodeId'] if k in data}
            tid=secrets.token_hex(16);idem=self.headers.get('Idempotency-Key')
            with db() as c:
                if idem:
                    prev=c.execute('SELECT * FROM tasks WHERE idem=?',(idem,)).fetchone()
                    if prev:return self.send(public_task(prev))
                c.execute('INSERT INTO tasks(id,payload,status,created,updated,idem) VALUES(?,?,?,?,?,?)',(tid,json.dumps(payload,ensure_ascii=False),'queued',time.time(),time.time(),idem))
            return self.send({'id':tid,'status':'queued'},201)
        if path=='/api/compose':
            files=data.get('files')
            if not isinstance(files,list) or not 1<=len(files)<=100 or not all(isinstance(x,str) and (valid_url(x) or safe_local(x)) for x in files):raise ValueError('files需要1—100个有效视频URL')
            tid=secrets.token_hex(16)
            with db() as c:c.execute('INSERT INTO tasks(id,payload,status,created,updated) VALUES(?,?,?,?,?)',(tid,json.dumps({'type':'compose','title':data.get('title','本集合成')}),'processing',time.time(),time.time()))
            threading.Thread(target=compose_task,args=(tid,data),daemon=True).start();return self.send({'id':tid,'status':'processing','title':data.get('title','本集合成')},201)
        self.send({'error':'Not found'},404)

if __name__=='__main__':
    if min(len(CLIENT),len(MUSE),len(SIGN))<24 or CLIENT==MUSE:raise SystemExit('设置独立 CLIENT_TOKEN、MUSE_TOKEN、SIGNING_KEY（各至少24字符），不可使用相同令牌')
    init();ThreadingHTTPServer((os.getenv('HOST','127.0.0.1'),int(os.getenv('PORT','8787'))),Handler).serve_forever()
