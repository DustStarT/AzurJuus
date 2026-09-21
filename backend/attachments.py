"""Bounded local uploads; UUIDs, not client paths, identify attachments."""
import base64
import json
import re
from pathlib import Path
from uuid import uuid4
from fastapi import HTTPException, Request
from .database import session_scope
from .models import Conversation

TYPES={'.png':'image/png','.jpg':'image/jpeg','.jpeg':'image/jpeg','.webp':'image/webp',
       '.pdf':'application/pdf','.docx':'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
       '.xlsx':'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
       '.txt':'text/plain','.md':'text/plain','.csv':'text/plain','.py':'text/plain','.json':'text/plain'}

def folder(settings):
    root=Path(settings.get('authorizedWorkspaceRoot') or '').resolve()
    if not settings.get('authorizedWorkspaceRoot') or not root.is_dir(): raise ValueError('请先配置授权工作区。')
    dest=root/'.juus-uploads'
    if dest.is_symlink() or (hasattr(dest,'is_junction') and dest.is_junction()): raise ValueError('上传目录不能是链接。')
    return dest

def resolve(ids,cid,settings):
    if not isinstance(ids,list) or len(ids)>4: raise ValueError('每条消息最多四个附件。')
    result=[]
    for aid in ids:
        if not isinstance(aid,str) or not re.fullmatch('[a-f0-9]{32}',aid): raise ValueError('附件编号无效。')
        root=folder(settings)
        metadata=root/(aid+'.json')
        if metadata.is_symlink(): raise ValueError('附件元数据无效。')
        meta=json.loads(metadata.read_text(encoding='utf-8'))
        if meta.get('suffix') not in TYPES: raise ValueError('附件类型无效。')
        if meta['conversationId']!=cid: raise ValueError('附件不属于此会话。')
        path=root/(aid+meta['suffix'])
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()): raise ValueError('附件路径无效。')
        result.append({**meta,'path':str(path)})
    return result

def image_parts(items):
    return [{'type':'image_url','image_url':{'url':'data:'+item['mime']+';base64,'+
        base64.b64encode(Path(item['path']).read_bytes()).decode('ascii')}} for item in items if item['mime'].startswith('image/')]

def install(app,coordinator):
    @app.get('/api/attachments/{attachment_id}')
    def download(attachment_id:str,conversationId:str):
        from fastapi.responses import FileResponse
        try: item=resolve([attachment_id],conversationId,coordinator.settings_loader())[0]
        except (OSError,ValueError,KeyError): raise HTTPException(404,'附件不存在。')
        return FileResponse(item['path'],media_type=item['mime'],filename=item['name'])

    @app.post('/api/attachments')
    async def upload(request:Request,conversationId:str,name:str):
        with session_scope() as session:
            room=session.get(Conversation,conversationId)
            if not room or (room.extra_json or {}).get('archived'): raise HTTPException(404,'会话不存在。')
        suffix=Path(name).suffix.lower()
        if suffix not in TYPES or len(name)>180: raise HTTPException(400,'暂不支持此文件类型。')
        content=bytearray()
        async for chunk in request.stream():
            content.extend(chunk)
            if len(content)>8*1024*1024: raise HTTPException(413,'单个附件不能超过8MB。')
        if not content: raise HTTPException(400,'附件为空。')
        mime=TYPES[suffix]
        if TYPES[suffix].startswith('image/'):
            from PIL import Image
            from io import BytesIO
            try:
                with Image.open(BytesIO(content)) as im:
                    if im.width*im.height>20_000_000: raise ValueError('图片尺寸过大')
                    mime=Image.MIME.get(im.format)
                    if mime not in {'image/png','image/jpeg','image/webp'}: raise ValueError('图片编码不支持')
                    im.verify()
            except Exception: raise HTTPException(400,'图片无效或尺寸过大。')
        try: dest=folder(coordinator.settings_loader())
        except ValueError as exc: raise HTTPException(400,str(exc))
        dest.mkdir(exist_ok=True)
        aid=uuid4().hex
        item={'id':aid,'name':Path(name).name,'suffix':suffix,'mime':mime,
              'conversationId':conversationId,'size':len(content)}
        (dest/(aid+suffix)).write_bytes(content)
        (dest/(aid+'.json')).write_text(json.dumps(item,ensure_ascii=False),encoding='utf-8')
        return item
