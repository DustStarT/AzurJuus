import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading

import pytest

from backend.capabilities import LocalCapabilities


@pytest.mark.asyncio
async def test_real_browser_local_form_download_and_grant(tmp_path):
    received=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def do_GET(self):
            self.send_response(200)
            if self.path == '/download':
                self.send_header('Content-Disposition','attachment; filename="evidence.txt"')
                self.end_headers()
                self.wfile.write(b'local download evidence')
            else:
                self.send_header('Content-Type','text/html; charset=utf-8')
                self.end_headers()
                self.wfile.write(b'<title>Local acceptance</title><form method="POST"><input name="note"><button id="submit">Submit</button></form><a id="download" href="/download">Download</a>')
        def do_POST(self):
            received.append(self.rfile.read(int(self.headers['Content-Length'])))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'<title>Saved</title>Saved successfully')
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    work=tmp_path/'work'; work.mkdir()
    tools=LocalCapabilities(tmp_path/'state')
    run={'id':'browser-test','workspace':str(work),'headless':True}
    async def execute(args):
        return await tools.execute(run,'browser',args,'call',lambda *a:None)
    try:
        page=await execute({'op':'navigate','url':f'http://127.0.0.1:{server.server_port}'})
        assert page['title']=='Local acceptance'
        await execute({'op':'fill','selector':'input[name=note]','value':'synthetic data'})
        assert not received
        assert tools.approval_reason(run,'browser',{'op':'submit','selector':'#submit'})
        # Explicitly authorized submission to the local fixture only.
        page=await execute({'op':'submit','selector':'#submit'})
        assert received == [b'note=synthetic+data']
        await execute({'op':'navigate','url':f'http://127.0.0.1:{server.server_port}'})
        assert tools.approval_reason(run,'browser',{'op':'download','selector':'#download','path':'download.txt'})
        await execute({'op':'download','selector':'#download','path':'download.txt'})
        assert (work/'download.txt').read_text()=='local download evidence'
    finally:
        await tools.close()
        server.shutdown(); server.server_close(); thread.join(timeout=2)
