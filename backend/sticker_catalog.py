"""Bounded official sticker catalog with lazy, locally cached Wiki media."""
import hashlib
import io
import json
import re
import time
from pathlib import Path
from urllib.parse import unquote,urlparse

import httpx
from bs4 import BeautifulSoup
from fastapi import HTTPException
from fastapi.responses import FileResponse
from PIL import Image, UnidentifiedImageError

from .config import get_settings

SOURCE='https://wiki.biligame.com/blhx/%E8%A1%A8%E6%83%85%E5%8C%85'
PACKAGED=Path(__file__).resolve().parents[1]/'resources/ui/wiki-assets.json'
MAX_MEDIA=3_000_000


def cache_dir():
    return get_settings().workspace_state_path.parent/'sticker-cache'


def media_url(url):
    parsed=urlparse(url)
    if parsed.scheme!='https' or parsed.hostname!='patchwiki.biligame.com' or not parsed.path.startswith('/images/blhx/'):
        raise ValueError('表情图片来源不在 Wiki 媒体域名内。')
    match=re.match(r'(/images/blhx)/thumb(/[^/]+/[^/]+/[^/]+\.(?:gif|png|webp))/\d+px-[^/]+$',parsed.path,re.I)
    path=match.group(1)+match.group(2) if match else parsed.path
    if not re.fullmatch(r'/images/blhx/[^/]+/[^/]+/[^/]+\.(?:gif|png|webp)',path,re.I):
        raise ValueError('表情文件路径无效。')
    return 'https://patchwiki.biligame.com'+path


def label_from_image(image):
    filename=unquote((image.get('name') or image.get('url','')).split('/')[-1])
    filename=re.sub(r'^\d+px-','',filename)
    match=re.search(r'^Emoji\d+-表情[：:]\s*(.+?)-?\.(?:gif|png)$',filename,re.I)
    if match:return match.group(1).rstrip('-').strip()
    match=re.search(r'^(?:动态表情包|官方动态表情包)_(.+?)\.(?:gif|png)$',filename,re.I)
    if match:return match.group(1).strip()
    return ''


def entries_from_images(images):
    entries=[];seen=set()
    for image in images:
        label=label_from_image(image)
        if not label or len(label)>32 or label in seen:continue
        try:url=media_url(image.get('url',''))
        except ValueError:continue
        seen.add(label)
        entries.append({'label':label,'source':SOURCE,'mediaUrl':url,
            'animated':url.lower().endswith('.gif')})
        if len(entries)>=120:break
    return entries


def _official_images(html):
    body=BeautifulSoup(html,'html.parser').select_one('.mw-parser-output')
    if body is None:raise ValueError('表情页面正文不可读取。')
    images=[]
    for heading in body.select('h2'):
        headline=heading.select_one('.mw-headline')
        raw=headline.get('id','') if headline else ''
        if raw.startswith('.'):
            try:title=bytes.fromhex(raw.replace('.','')).decode('utf-8')
            except ValueError:title=headline.get_text(' ',strip=True)
        else:title=headline.get_text(' ',strip=True) if headline else ''
        if not (title=='新表情' or title.startswith('官方') and '表情' in title):continue
        for sibling in heading.next_siblings:
            if getattr(sibling,'name',None)=='h2':break
            if not hasattr(sibling,'select'):continue
            for image in sibling.select('img'):
                images.append({'name':image.get('alt',''),
                    'url':image.get('data-src') or image.get('src','')})
    return images


def catalog():
    saved=cache_dir()/'catalog.json'
    if saved.is_file():
        try:return json.loads(saved.read_text(encoding='utf-8'))['entries']
        except (ValueError,KeyError,OSError):pass
    return entries_from_images(json.loads(PACKAGED.read_text(encoding='utf-8'))['images'])


def labels(limit=16):
    return [entry['label'] for entry in catalog()[:limit]]


async def refresh():
    async with httpx.AsyncClient(timeout=20,follow_redirects=False) as client:
        response=await client.get(SOURCE)
        response.raise_for_status()
    if len(response.content)>2_000_000:raise ValueError('表情目录页面过大。')
    entries=entries_from_images(_official_images(response.content))
    if not entries:raise ValueError('表情目录没有可核验的官方条目。')
    folder=cache_dir();folder.mkdir(parents=True,exist_ok=True)
    target=folder/'catalog.json';temporary=folder/'catalog.tmp'
    temporary.write_text(json.dumps({'source':SOURCE,'checkedAt':time.time(),'entries':entries},ensure_ascii=False),encoding='utf-8')
    temporary.replace(target)
    return entries


async def media(label,still=False):
    entry=next((item for item in catalog() if item['label']==label),None)
    if label=='标枪疑惑':
        suffix='png' if still else 'gif'
        return FileResponse(Path(__file__).resolve().parents[1]/f'resources/ui/javelin.{suffix}',media_type=f'image/{suffix}')
    if entry is None:raise HTTPException(404,'表情不在已核对目录中。')
    folder=cache_dir();suffix='.gif' if entry['animated'] else '.png'
    filename=hashlib.sha256(entry['mediaUrl'].encode()).hexdigest()+suffix
    target=folder/filename
    if not target.is_file():
        try:
            async with httpx.AsyncClient(timeout=15,follow_redirects=False) as client:
                async with client.stream('GET',entry['mediaUrl']) as response:
                    response.raise_for_status();buffer=bytearray()
                    async for chunk in response.aiter_bytes():
                        buffer.extend(chunk)
                        if len(buffer)>MAX_MEDIA:raise ValueError('表情文件超出大小限制。')
            with Image.open(io.BytesIO(buffer)) as image:
                if image.format not in {'GIF','PNG','WEBP'} or image.width*image.height>1_048_576:
                    raise ValueError('表情图像格式或尺寸无效。')
                image.verify()
            folder.mkdir(parents=True,exist_ok=True)
            temporary=folder/(filename+'.tmp')
            temporary.write_bytes(buffer);temporary.replace(target)
        except (httpx.HTTPError,UnidentifiedImageError,ValueError) as exc:
            raise HTTPException(503,'表情暂时无法从 Wiki 下载。') from exc
    if still and entry['animated']:
        still_target=folder/(filename+'.still.png')
        if not still_target.is_file():
            with Image.open(target) as image:
                frame=image.convert('RGBA')
                frame.save(still_target.with_suffix('.tmp'),'PNG')
            still_target.with_suffix('.tmp').replace(still_target)
        target=still_target;suffix='.png'
    return FileResponse(target,media_type='image/gif' if suffix=='.gif' else 'image/png',
        headers={'Cache-Control':'private, max-age=86400'})


def install(app):
    from fastapi import HTTPException
    @app.get('/api/stickers')
    def listing():
        return {'source':SOURCE,'stickers':[{'label':e['label'],'animated':e['animated']} for e in catalog()]}
    @app.post('/api/stickers/refresh')
    async def refresh_index():
        try:entries=await refresh()
        except (httpx.HTTPError,ValueError) as exc:raise HTTPException(503,'Wiki 表情目录暂时不可读取，保留本地目录。') from exc
        return {'count':len(entries),'source':SOURCE}
    @app.get('/api/stickers/{label}')
    async def sticker_file(label:str,still:bool=False):
        return await media(label,still=still)
