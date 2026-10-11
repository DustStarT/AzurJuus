"""Local scanned-page OCR with bounded rendering and explicit source/confidence."""
import hashlib
import threading
from pathlib import Path

_lock=threading.Lock()
_engine=None


def read_pdf(path: Path, offset: int, limit: int):
    from pypdf import PdfReader
    reader=PdfReader(path)
    if reader.is_encrypted and not reader.decrypt(''):
        raise ValueError('PDF 已加密，需要先提供可读取的副本。')
    total=len(reader.pages)
    # OCR never processes unrequested pages. Resume at nextOffset for scans.
    end=min(total,offset+min(limit,10))
    items=[]
    for index in range(offset,end):
        text=reader.pages[index].extract_text() or ''
        if text.strip():
            items.append({'page':index+1,'text':text,'needsOCR':False,'source':'pdf-text'})
            continue
        try:
            lines=scan_page(path,index)
        except Exception as exc:
            raise ValueError('PDF 第 '+str(index+1)+' 页本地 OCR 失败：'+type(exc).__name__) from exc
        items.append({'page':index+1,'text':'\n'.join(line['text'] for line in lines),
            'needsOCR':not bool(lines),'source':'local-ocr','confidence':min((l['confidence'] for l in lines),default=0),
            'lines':lines,'warning':'OCR 识别可能有误，请核对低置信度文字。' if lines else '该页没有识别到文字。'})
    return {'path':str(path),'items':items,'total':total,'nextOffset':end if end<total else None,
        'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}


def scan_page(path, index):
    global _engine
    import pypdfium2 as pdfium
    import numpy as np
    from rapidocr_onnxruntime import RapidOCR
    # PDFium and the shared inference object are not concurrently reentered.
    with _lock:
        with pdfium.PdfDocument(str(path)) as document:
            page=document[index]
            try:
                width,height=page.get_size()
                if width<=0 or height<=0:raise ValueError('PDF 页面尺寸无效。')
                scale=min(2.5,3000/max(width,height),(10_000_000/(width*height))**.5)
                bitmap=page.render(scale=scale)
                try:image=np.asarray(bitmap.to_pil().convert('RGB')).copy()
                finally:bitmap.close()
            finally:page.close()
        if _engine is None:
            _engine=RapidOCR(intra_op_num_threads=2,inter_op_num_threads=1)
        result,_=_engine(image)
        return [{'text':line[1],'confidence':round(float(line[2]),4),'box':[[float(v) for v in point] for point in line[0]]}
            for line in result or []]
