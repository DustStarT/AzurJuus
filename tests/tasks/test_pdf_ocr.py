from pathlib import Path
import pytest
from PIL import Image,ImageDraw,ImageFont
from backend.tasks.pdf_ocr import read_pdf


def test_real_local_ocr_reads_bilingual_scanned_pdf_and_paginates(tmp_path):
    font_path=Path('C:/Windows/Fonts/msyh.ttc')
    if not font_path.exists():pytest.skip('Bilingual fixture needs the verified Windows font')
    pages=[]
    for code in ['PORT-834729','PORT-592681']:
        image=Image.new('RGB',(1000,500),'white')
        draw=ImageDraw.Draw(image);font=ImageFont.truetype(str(font_path),44)
        draw.text((60,50),'扫描文档测试',font=font,fill='black')
        draw.text((60,170),code,font=font,fill='black')
        pages.append(image)
    path=tmp_path/'scan.pdf';pages[0].save(path,save_all=True,append_images=pages[1:],resolution=120)
    first=read_pdf(path,0,1)
    assert first['total']==2 and first['nextOffset']==1 and len(first['items'])==1
    row=first['items'][0]
    assert row['source']=='local-ocr' and row['needsOCR'] is False and row['confidence']>0
    assert '扫描文档测试' in row['text'].replace(' ','') and 'PORT-834729' in row['text'].replace(' ','')
    assert first['sha256'] and row['lines'][0]['box']
    second=read_pdf(path,1,1)
    assert second['nextOffset'] is None and second['items'][0]['page']==2
    assert 'PORT-592681' in second['items'][0]['text'].replace(' ','')


def test_blank_encrypted_and_invalid_pdf_are_explicit(tmp_path):
    from pypdf import PdfWriter
    path=tmp_path/'blank.pdf'
    writer=PdfWriter();writer.add_blank_page(width=300,height=200)
    with path.open('wb') as f:writer.write(f)
    row=read_pdf(path,0,1)['items'][0]
    assert row['needsOCR'] is True and row['text']=='' and row['confidence']==0
    writer.encrypt('synthetic-password')
    with path.open('wb') as f:writer.write(f)
    with pytest.raises(ValueError,match='加密'):read_pdf(path,0,1)
    path.write_bytes(b'not a pdf')
    with pytest.raises(Exception):read_pdf(path,0,1)


def test_read_document_uses_local_ocr_with_vision_disabled(tmp_path):
    from backend.tasks.capabilities import LocalCapabilities
    image=Image.new('RGB',(700,220),'white')
    ImageDraw.Draw(image).text((35,65),'INVOICE 73182',font=ImageFont.truetype('arial.ttf',42),fill='black')
    work=tmp_path/'work';work.mkdir();image.save(work/'invoice.pdf')
    tools=LocalCapabilities(tmp_path/'state')
    result=tools.file_tool({'workspace':str(work),'visionEnabled':False},'read_document',{'path':'invoice.pdf'})
    assert '73182' in result['items'][0]['text'] and result['items'][0]['source']=='local-ocr'
