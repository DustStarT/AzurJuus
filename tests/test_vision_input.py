import asyncio
import base64
import hashlib
import io
from pathlib import Path
import pytest
from PIL import Image
from backend.capabilities import LocalCapabilities,CapabilityError,phase_actions
from backend.hermes_bridge import HermesBridge


def test_read_image_preserves_source_evidence_and_permissions(tmp_path):
    source=tmp_path/'sample.jpg'
    Image.new('RGB',(32,24),'blue').save(source)
    tools=LocalCapabilities(tmp_path/'runtime')
    run={'workspace':str(tmp_path),'visionEnabled':True}
    result=tools.file_tool(run,'read_image',{'path':'sample.jpg'})
    assert result['sha256']==hashlib.sha256(source.read_bytes()).hexdigest()
    assert result['bytes']==source.stat().st_size
    with Image.open(io.BytesIO(base64.b64decode(result['image']))) as im:
        assert im.size==(32,24) and im.format=='PNG'
    assert 'read_image' in phase_actions('reviewer')
    assert 'read_image' not in phase_actions('planner')
    with pytest.raises(CapabilityError):tools.file_tool({**run,'visionEnabled':False},'read_image',{'path':'sample.jpg'})
    with pytest.raises(CapabilityError):tools.file_tool(run,'read_image',{'path':'../outside.png'})


@pytest.mark.asyncio
async def test_bridge_attaches_before_first_prompt_only(tmp_path):
    async def event(*args):pass
    bridge=HermesBridge(tmp_path,{'llmModel':'deepseek-flash','_inputImages':['a.png']},'local','token',event)
    calls=[]
    async def rpc(method,params):
        calls.append((method,params))
        if method=='session.create':return {'session_id':'test'}
        if method=='prompt.submit':await bridge.completed.put({'status':'ok','text':'answer'})
        return {}
    bridge.rpc=rpc
    assert await bridge.prompt('看图片',str(tmp_path))=='answer'
    await bridge.prompt('核对',str(tmp_path))
    assert [m for m,_ in calls]==['session.create','image.attach','prompt.submit','prompt.submit']
    assert calls[1][1]=={'session_id':'test','path':'a.png'}
