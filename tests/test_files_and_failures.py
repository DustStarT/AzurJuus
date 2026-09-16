import asyncio
import sys
import threading

import pytest
from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject

from backend.capabilities import LocalCapabilities, CapabilityError
from backend.llm_runtime import AgentRuntime
from backend.sqlite_policy import journal_mode


@pytest.fixture
def files(tmp_path):
    work=tmp_path/'work';work.mkdir()
    return LocalCapabilities(tmp_path/'state'),{'id':'files','workspace':str(work)},work


@pytest.mark.asyncio
async def test_mixed_files_move_overwrite_undo_restore(files):
    tools,run,work=files
    (work/'one.txt').write_text('original source')
    (work/'folder').mkdir()
    (work/'folder'/'one.txt').write_text('original destination')
    result=tools.file_tool(run,'move',{'src':'one.txt','dest':'folder/one.txt'})
    assert not (work/'one.txt').exists()
    assert (work/'folder/one.txt').read_text()=='original source'
    assert tools.approval_reason(run,'undo',{'snapshotId':result['snapshotId']})
    tools.file_tool(run,'undo',{'snapshotId':result['snapshotId']})
    assert (work/'one.txt').read_text()=='original source'
    assert (work/'folder/one.txt').read_text()=='original destination'
    with pytest.raises(CapabilityError):
        tools.file_tool(run,'undo',{'snapshotId':result['snapshotId']})
    updated=tools.file_tool(run,'write_file',{'path':'one.txt','content':'updated'})
    tools.file_tool(run,'restore',{'path':'one.txt','snapshotId':updated['snapshotId']})
    assert (work/'one.txt').read_text()=='original source'


def test_long_pdf_docx_and_text_source_locations(files):
    tools,run,work=files
    writer=PdfWriter()
    for i in range(12):
        page=writer.add_blank_page(width=300,height=300)
        font=DictionaryObject({NameObject('/Type'):NameObject('/Font'),NameObject('/Subtype'):NameObject('/Type1'),NameObject('/BaseFont'):NameObject('/Helvetica')})
        page[NameObject('/Resources')]=DictionaryObject({NameObject('/Font'):DictionaryObject({NameObject('/F1'):font})})
        stream=DecodedStreamObject();stream.set_data(f'BT /F1 12 Tf 20 200 Td (Source page {i+1}) Tj ET'.encode())
        page[NameObject('/Contents')]=writer._add_object(stream)
    writer.write(work/'source.pdf')
    pdf=tools.file_tool(run,'read_document',{'path':'source.pdf','offset':7,'limit':2})
    assert [i['page'] for i in pdf['items']]==[8,9]
    assert 'Source page 8' in pdf['items'][0]['text']
    assert pdf['nextOffset']==9
    tools.file_tool(run,'write_document',{'path':'source.docx','content':'\n'.join(f'Paragraph {i}' for i in range(700))})
    doc=tools.file_tool(run,'read_document',{'path':'source.docx','offset':650,'limit':100})
    assert len(doc['items'])==50 and doc['items'][0]['paragraph']==651
    tools.file_tool(run,'write_file',{'path':'long.txt','content':'\n'.join(str(i) for i in range(1200))})
    text=tools.file_tool(run,'read_file',{'path':'long.txt','offset':1100,'limit':200})
    assert text['content'].startswith('1101: 1100') and text['nextOffset'] is None
    (work/'broken.pdf').write_text('not a PDF')
    with pytest.raises(Exception): tools.file_tool(run,'read_document',{'path':'broken.pdf'})


@pytest.mark.asyncio
async def test_program_test_failure_then_fix_and_timeout(files):
    tools,run,work=files
    tools.file_tool(run,'write_file',{'path':'sample.py','content':'def add(a,b): return a-b\nassert add(2,3)==5\n'})
    first=await tools.execute(run,'command',{'argv':[sys.executable,'sample.py']},'fail',lambda *a:None)
    assert first['exitCode']!=0 and first['success'] is False
    tools.file_tool(run,'patch_file',{'path':'sample.py','old':'return a-b','new':'return a+b'})
    second=await tools.execute(run,'command',{'argv':[sys.executable,'sample.py']},'pass',lambda *a:None)
    assert second['exitCode']==0
    with pytest.raises(TimeoutError):
        await tools.execute(run,'command',{'argv':[sys.executable,'-c','import time;time.sleep(15)'],'timeout':1},'timeout',lambda *a:None)
    assert not tools.processes


def test_context_retains_empty_tool_request_and_response():
    runtime=AgentRuntime(1)
    messages=[{'role':'system','content':'rules'},{'role':'user','content':'old '*10000},{'role':'assistant','content':None,'tool_calls':[{'id':'call','type':'function','function':{'name':'read','arguments':'{}'}}]},{'role':'tool','tool_call_id':'call','content':'result'}]
    trimmed=runtime._trim_messages(messages,300)
    assert [m['role'] for m in trimmed]==['system','assistant','tool']
    assert trimmed[1]['tool_calls'][0]['id']==trimmed[2]['tool_call_id']


def test_unpatched_sqlite_uses_rollback_journal():
    assert journal_mode((3,51,2))=='DELETE'
    assert journal_mode((3,51,3))=='WAL'
    assert journal_mode((3,50,7))=='WAL'
    assert journal_mode((3,44,6))=='WAL'


def test_single_long_line_can_be_read_to_the_end(files):
    tools,run,work=files
    expected='x'*40000+'END'
    (work/'single.txt').write_text(expected)
    offset=column=0
    actual=''
    while True:
        page=tools.file_tool(run,'read_file',{'path':'single.txt','offset':offset,'column':column,'maxChars':1000})
        actual+=page['content'].split(': ',1)[1]
        if page['nextOffset'] is None:break
        offset,column=page['nextOffset'],page['nextColumn']
    assert actual==expected
