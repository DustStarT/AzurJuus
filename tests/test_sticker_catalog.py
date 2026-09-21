import asyncio
import hashlib

import pytest
from PIL import Image
from test_cognition import world

from backend.sticker_catalog import _official_images, catalog, entries_from_images, label_from_image, media_url


def test_only_official_wiki_sections_enter_live_catalog():
    html='''<div class="mw-parser-output">
      <h2><span class="mw-headline" id="新表情">新表情</span></h2>
      <div><img alt="Emoji33-表情：惊了-.png" src="https://patchwiki.biligame.com/images/blhx/a/b/Emoji33-%E8%A1%A8%E6%83%85%EF%BC%9A%E6%83%8A%E4%BA%86-.png"></div>
      <h2><span class="mw-headline" id="玩家自制">玩家自制</span></h2>
      <div><img alt="Emoji34-表情：假冒-.png" src="https://patchwiki.biligame.com/images/blhx/a/b/Emoji34-%E8%A1%A8%E6%83%85%EF%BC%9A%E5%81%87%E5%86%92-.png"></div>
    </div>'''
    entries=entries_from_images(_official_images(html))
    assert [entry['label'] for entry in entries]==['惊了']


def test_sticker_media_is_allowlisted_and_thumbnail_resolves_to_original():
    url='https://patchwiki.biligame.com/images/blhx/thumb/a/b/hello.gif/120px-hello.gif'
    assert media_url(url)=='https://patchwiki.biligame.com/images/blhx/a/b/hello.gif'
    with pytest.raises(ValueError):media_url('https://example.com/images/blhx/a/b/hello.gif')
    with pytest.raises(ValueError):media_url('https://patchwiki.biligame.com/images/blhx/a/b/../../secret.gif')


def test_packaged_catalog_contains_animated_and_static_labels():
    entries=catalog()
    assert entries
    assert any(entry['animated'] for entry in entries)
    assert any(not entry['animated'] for entry in entries)
    assert label_from_image({'name':'Emoji33-表情：惊了-.png'})=='惊了'


def test_animated_sticker_has_still_frame_without_network(monkeypatch,tmp_path):
    from backend import sticker_catalog as module
    url='https://patchwiki.biligame.com/images/blhx/a/b/test.gif'
    entry={'label':'测试表情','mediaUrl':url,'animated':True}
    monkeypatch.setattr(module,'catalog',lambda:[entry])
    monkeypatch.setattr(module,'cache_dir',lambda:tmp_path)
    original=tmp_path/(hashlib.sha256(url.encode()).hexdigest()+'.gif')
    Image.new('RGB',(2,2),'red').save(original,'GIF')
    response=asyncio.run(module.media('测试表情',still=True))
    assert response.media_type=='image/png'
    with Image.open(response.path) as image:
        assert image.size==(2,2)


def test_sticker_api_lists_validated_names_without_fetching_media(world):
    _,client,_=world
    response=client.get('/api/stickers')
    assert response.status_code==200
    assert any(item['animated'] for item in response.json()['stickers'])
    assert client.get('/api/stickers/不存在的表情').status_code==404
