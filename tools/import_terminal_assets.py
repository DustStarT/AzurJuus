"""Cache selected Wiki-hosted official assets with source attribution."""
import json
import time
from pathlib import Path
from urllib.parse import quote
import httpx
from bs4 import BeautifulSoup

ROOT=Path(__file__).resolve().parents[1]/'resources/ui'
def get(url):
    for attempt in range(3):
        response=httpx.get(url,timeout=25,headers={'User-Agent':'Mozilla/5.0'},follow_redirects=False)
        if response.is_success:return response.content
        if attempt<2:time.sleep(1)
    response.raise_for_status()

def main():
    ROOT.mkdir(parents=True,exist_ok=True)
    source='https://wiki.biligame.com/blhx/'+quote('表情包')
    # These individual official files were resolved from the source page's links.
    selected={'javelin':'https://patchwiki.biligame.com/images/blhx/c/c7/4uxymdps8g137nzjtdaswg9nliyuoha.gif'}
    for name,url in selected.items():
        (ROOT/(name+'.gif')).write_bytes(get(url))
    from PIL import Image
    Image.open(ROOT/'javelin.gif').convert('RGBA').save(ROOT/'javelin.png')
    (ROOT/'asset-sources.json').write_text(json.dumps({'source':source,'hubIcon':'port-anchor.svg is an application-drawn anchor, not an extracted official logo','officialStickers':selected},ensure_ascii=False,indent=2),encoding='utf-8')
    try:html=get(source)
    except httpx.HTTPError:
        print('Selected official assets cached; full Wiki gallery temporarily unavailable.')
        return
    soup=BeautifulSoup(html.decode('utf-8'),'html.parser')
    body=soup.select_one('.mw-parser-output')
    images=[{'name':i.get('alt',''),'url':i.get('data-src') or i.get('src','')} for i in body.select('img')]
    (ROOT/'wiki-assets.json').write_text(json.dumps({'source':source,'images':images},ensure_ascii=False,indent=2),encoding='utf-8')
    print('Official sticker cached; gallery images:',len(images))

if __name__=='__main__':main()
