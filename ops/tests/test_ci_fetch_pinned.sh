#!/usr/bin/env bash
# The smart-HTTP peer is real Git; only the fault at the transport/process boundary is injected.
set -eu
python3 - "$(cd "$(dirname "$0")/../.." && pwd)/ops/ci-fetch-pinned" "$@" <<'PY'
import http.server, os, pathlib, socket, socketserver, subprocess, sys, tempfile, threading, time

helper = pathlib.Path(sys.argv[1]).resolve()
selected = sys.argv[2:]
real_git = subprocess.check_output(['sh', '-c', 'command -v git'], text=True).strip()
def git(*args, **kwargs):
    return subprocess.check_output([real_git, *map(str,args)], stderr=subprocess.DEVNULL, **kwargs)

def check(case):
    # intent: retries never change the requested revision; permanent failures consume one attempt.
    with tempfile.TemporaryDirectory() as scratch:
        root = pathlib.Path(scratch)
        repo = root/'GEN/gen-mcp-server.git'
        repo.parent.mkdir()
        git('init','-q',repo)
        git('-C',repo,'config','user.name','fixture')
        git('-C',repo,'config','user.email','fixture@example.invalid')
        git('-C',repo,'config','uploadpack.allowAnySHA1InWant','true')
        (repo/'file').write_text('first')
        git('-C',repo,'add','file'); git('-C',repo,'commit','-qm','first')
        sha=git('-C',repo,'rev-parse','HEAD',text=True).strip()
        (repo/'file').write_text('second')
        git('-C',repo,'commit','-qam','second')
        other=git('-C',repo,'rev-parse','HEAD',text=True).strip()
        state={'attempts':0}
        class Peer(http.server.BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def do_GET(self): self.serve()
            def do_POST(self): self.serve()
            def serve(self):
                is_start=self.path.endswith('/info/refs?service=git-upload-pack')
                if is_start: state['attempts']+=1
                if case=='T6':
                    self.send_response(401); self.send_header('Content-Length','0'); self.end_headers(); return
                body=self.rfile.read(int(self.headers.get('Content-Length',0)))
                if self.command=='POST' and ((case=='T1' and state['attempts']==1) or case=='T3'):
                    self.send_response(200); self.send_header('Content-Type','application/x-git-upload-pack-result'); self.end_headers()
                    time.sleep(60); return
                if self.command=='POST' and case=='T2' and state['attempts']==1:
                    self.connection.shutdown(socket.SHUT_RDWR); self.connection.close(); return
                path,_,query=self.path.partition('?')
                env=dict(os.environ,GIT_PROJECT_ROOT=str(root),GIT_HTTP_EXPORT_ALL='1',REQUEST_METHOD=self.command,
                         PATH_INFO=path,QUERY_STRING=query,CONTENT_TYPE=self.headers.get('Content-Type',''),
                         CONTENT_LENGTH=str(len(body)),REMOTE_USER='fixture')
                output=subprocess.run([real_git,'http-backend'],input=body,env=env,capture_output=True).stdout
                headers,body=output.split(b'\r\n\r\n',1)
                self.send_response(200)
                for line in headers.decode().split('\r\n'):
                    name,value=line.split(':',1)
                    if name.lower()!='status': self.send_header(name,value.strip())
                self.send_header('Content-Length',str(len(body))); self.end_headers()
                try: self.wfile.write(body)
                except (BrokenPipeError,ConnectionResetError): pass
        stopped=threading.Event()
        class StalledTLS(socketserver.BaseRequestHandler):
            def handle(self):
                state['attempts']+=1
                state.setdefault('accepted_at',[]).append(time.monotonic())
                stopped.wait(175)
        if case=='T8':
            class TLSPeer(socketserver.ThreadingTCPServer):
                daemon_threads=True
            server=TLSPeer(('127.0.0.1',0),StalledTLS)
        else:
            server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Peer)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        config=root/'gitconfig'
        scheme='https' if case=='T8' else 'http'
        config.write_text('[url "%s://127.0.0.1:%d/"]\n insteadOf = https://git.gen.pro/\n' % (scheme,server.server_address[1]))
        env=dict(os.environ,GIT_CONFIG_GLOBAL=str(config),GIT_CONFIG_NOSYSTEM='1',GEN_GITEA_READ_TOKEN='fixture-token')
        if case=='T5':
            # Git rejects an invalid pack itself. Inject a successful wrong fetch at the Git boundary
            # to prove the helper also refuses stale/wrong FETCH_HEAD rather than trusting exit zero.
            bindir=root/'bin'; bindir.mkdir()
            wrapper=bindir/'git'
            wrapper.write_text('#!/usr/bin/env bash\nargs=("$@"); for ((i=0;i<${#args[@]};i++)); do if [[ ${args[i]} == '+sha+' && " $* " == *" fetch "* ]]; then args[i]='+other+'; fi; done\nexec '+real_git+' "${args[@]}"\n')
            wrapper.chmod(0o755); env['PATH']=str(bindir)+':'+env['PATH']
        requested='1'*40 if case=='T4' else sha
        dest=root/'dest'; started=time.monotonic()
        try:
            p=subprocess.run([str(helper),'gen-mcp-server',requested,str(dest)],env=env,capture_output=True,text=True,timeout=175)
            elapsed=time.monotonic()-started
            expected={'T3':f'PINNED_FETCH_STALLED gen-mcp-server {sha} after 3 attempts',
                      'T8':f'PINNED_FETCH_STALLED gen-mcp-server {sha} after 3 attempts',
                      'T4':f'PINNED_FETCH_MISSING gen-mcp-server {requested}',
                      'T5':'PINNED_FETCH_MISMATCH','T6':'PINNED_FETCH_AUTH'}
            if case in ('T1','T2'):
                assert p.returncode==0,(p.stdout,p.stderr)
                assert git('-C',dest,'rev-parse','HEAD',text=True).strip()==sha
                assert state['attempts']==2,state
                assert 'attempt 2/3 ok' in p.stdout,p.stdout
                if case=='T1': assert elapsed<55,elapsed
            else:
                assert p.returncode==1,(p.returncode,p.stdout,p.stderr)
                assert expected[case] in p.stderr,p.stderr
                assert state['attempts']==(3 if case in ('T3','T8') else 1),state
                if case in ('T3','T8'): assert 100<=elapsed<180,elapsed
                if case=='T8':
                    a,b,c=state['accepted_at']
                    durations=[b-a-5,c-b-15,time.monotonic()-c]
                    assert all(29<=duration<35 for duration in durations),durations
            print(f'{case} PASS attempts={state["attempts"]} elapsed={elapsed:.2f}s',flush=True)
        finally: stopped.set(); server.shutdown(); server.server_close()

for case in selected or ['T1','T2','T3','T4','T5','T6','T8']:
    check(case)
PY
