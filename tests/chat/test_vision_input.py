import base64
import hashlib
import io
import pytest
from PIL import Image
from backend.tasks.capabilities import LocalCapabilities,CapabilityError,phase_actions


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
