#!/usr/bin/env python3
"""Autonomous MinerU v4 OCR; compact events on stdout, details in a file."""
import argparse
import csv
import hashlib
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import unicodedata
import zipfile
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

import fitz
import requests
from pypdf import PdfReader, PdfWriter

API = 'https://mineru.net/api/v4'
LIMIT = 200_000_000
LOG = logging.getLogger('ocr')
TOKEN = ''
LAST_EVENT = 0.0

class NeedsDecision(RuntimeError): pass
class AuthenticationError(RuntimeError): pass
class Paused(RuntimeError): pass


def safe_message(value):
    text = str(value)
    if TOKEN:
        text = text.replace(TOKEN, '[REDACTED]')
    text = re.sub(r'https?://[^\s\"\'<>]+', '[URL omitted]', text)
    text = re.sub(r'(?i)Bearer\s+\S+', 'Bearer [REDACTED]', text)
    return text[:2000]


def emit(event, force=False, **data):
    global LAST_EVENT
    now = time.monotonic()
    LOG.info('%s %s', event, json.dumps(data, ensure_ascii=False))
    if force or now - LAST_EVENT >= 60:
        print(json.dumps({'event': event, **data}, ensure_ascii=False), flush=True)
        LAST_EVENT = now


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(path)


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8'))


def sha256(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for part in iter(lambda: f.read(1024 * 1024), b''):
            h.update(part)
    return h.hexdigest()


def check_pause(args):
    if args.pause_file.exists():
        raise Paused('暂停标记存在；整条管线已停止，恢复前由用户移除标记')


def wait(seconds, args):
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        check_pause(args)
        time.sleep(min(1, max(0, until - time.monotonic())))


def safe_path(root, relative):
    p = PurePosixPath(str(relative).replace('\\', '/'))
    if p.is_absolute() or '..' in p.parts or not p.parts or ':' in p.parts[0]:
        raise NeedsDecision('不安全的相对路径')
    target = root.joinpath(*p.parts)
    if root.resolve() not in target.resolve().parents:
        raise NeedsDecision('路径超出任务目录')
    return target


def validate_hashes(root, entries):
    if not entries:
        return False
    try:
        return all(safe_path(root, name).is_file() and sha256(safe_path(root, name)) == digest
                   for name, digest in entries.items())
    except (OSError, NeedsDecision):
        return False


def inventory(inp, out, recursive=True, excluded=()):
    inp=inp.expanduser().resolve()
    out=out.expanduser().resolve()
    excluded=[p.expanduser().resolve() for p in excluded]
    if not inp.exists():
        raise ValueError('输入不存在')
    if out == inp or (inp.is_file() and inp in out.parents):
        raise ValueError('输出路径与输入冲突')
    if inp.is_file():
        paths = [inp]
    else:
        paths = inp.rglob('*') if recursive else inp.iterdir()
    result = []
    for p in paths:
        if p.name.startswith('._') or p.suffix.lower() != '.pdf' or not p.is_file():
            continue
        p = p.resolve()
        if p == out or out in p.parents or any(p == e or e in p.parents for e in excluded):
            continue
        result.append(p)
    return sorted(set(result))


def chunks(reader, source, work):
    total = len(reader.pages)
    if not total:
        raise ValueError('空 PDF')
    if total <= 200 and source.stat().st_size <= LIMIT:
        return [(0, total, source)]
    work.mkdir(parents=True, exist_ok=True)
    result = []
    def write(a, b):
        dest = safe_path(work, f'pages_{a+1:06d}_{b:06d}.pdf')
        # Rebuild owned temporary files; never trust an interrupted partial PDF.
        writer = PdfWriter()
        for n in range(a, b):
            writer.add_page(reader.pages[n])
        with dest.open('wb') as f:
            writer.write(f)
        if dest.stat().st_size > LIMIT:
            dest.unlink()
            if b-a == 1:
                raise NeedsDecision(f'单页 {a+1} 超过上传大小限制')
            m = (a+b)//2
            write(a, m); write(m, b)
        else:
            result.append((a, b, dest))
    for a in range(0, total, 200):
        write(a, min(a+200, total))
    if [p for a,b,_ in result for p in range(a,b)] != list(range(total)):
        raise NeedsDecision('切块覆盖不完整')
    return result


def api(session, method, endpoint, args, **kw):
    tries = args.network_retries + 1 if method == 'GET' else 1
    for attempt in range(tries):
        check_pause(args)
        try:
            r = session.request(method, API + endpoint,
                                headers={'Authorization': 'Bearer ' + TOKEN},
                                timeout=(30, 90), **kw)
            if r.status_code in (401,403):
                raise AuthenticationError('MinerU 认证/权限失败，已停止整批上传')
            if r.status_code == 429 or r.status_code >= 500:
                raise requests.ConnectionError(f'API HTTP {r.status_code}')
            if not r.ok:
                raise NeedsDecision(f'API HTTP {r.status_code}')
            obj = r.json()
            if obj.get('code') != 0:
                raise NeedsDecision('MinerU API 错误：' + safe_message(obj.get('msg', obj.get('code'))))
            if not isinstance(obj.get('data'), dict):
                raise NeedsDecision('API data 格式不兼容')
            return obj['data']
        except (requests.RequestException, ValueError) as exc:
            LOG.warning('API %s attempt %s: %s', method, attempt+1, safe_message(exc))
            if attempt+1 == tries:
                raise NeedsDecision('网络操作未确认；保留状态，需判断后续跑') from None
            wait(min(2**attempt, 15), args)


def download(session, url, dest, args):
    for attempt in range(args.network_retries+1):
        check_pause(args)
        tmp = dest.with_name(dest.name+'.part')
        try:
            # No API authorization header is passed to the signed CDN URL.
            with session.get(url, stream=True, timeout=(30,600)) as r:
                if not r.ok:
                    raise requests.ConnectionError(f'下载 HTTP {r.status_code}')
                with tmp.open('wb') as f:
                    for data in r.iter_content(1024*1024):
                        check_pause(args)
                        f.write(data)
            with zipfile.ZipFile(tmp) as z:
                if z.testzip():
                    raise ValueError('ZIP 校验失败')
            tmp.replace(dest)
            return
        except (requests.RequestException, zipfile.BadZipFile, ValueError) as exc:
            LOG.warning('下载 attempt %s: %s', attempt+1, safe_message(exc))
            tmp.unlink(missing_ok=True)
            if attempt == args.network_retries:
                raise NeedsDecision('结果下载失败；远端批次保留，可继续下载') from None
            wait(min(2**attempt,15), args)


def unpack(zpath, cache):
    if cache.exists():
        shutil.rmtree(cache)
    cache.mkdir(parents=True)
    with zipfile.ZipFile(zpath) as z:
        files = [n for n in z.namelist() if not n.endswith('/') and not n.startswith('__MACOSX/')]
        if len(files) != len(set(files)):
            raise NeedsDecision('ZIP 存在重名成员')
        md = [n for n in files if PurePosixPath(n).name == 'full.md']
        cl = [n for n in files if PurePosixPath(n).name == 'content_list.json' or n.endswith('_content_list.json')]
        if len(md)!=1 or len(cl)!=1:
            raise NeedsDecision('需要一个 full.md 和 V1 content_list.json；结果格式不兼容')
        base = PurePosixPath(md[0]).parent
        for name in files:
            safe_path(cache, name)  # Check every member even if not retained.
        blocks = json.loads(z.read(cl[0]))
        if isinstance(blocks, dict): blocks = blocks.get('content_list')
        if not isinstance(blocks, list):
            raise NeedsDecision('仅支持 V1 页级内容列表；结果格式需判断')
        (cache/'full.md').write_bytes(z.read(md[0]))
        save_json(cache/'content_list.json', blocks)
        for name in files:
            p = PurePosixPath(name)
            if 'images' in p.parts:
                # Preserve relative image references, without changing the result hierarchy.
                try: rel = p.relative_to(base)
                except ValueError: raise NeedsDecision('图片位于 Markdown 根目录以外')
                dest = safe_path(cache, rel)
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(z.read(name))
            elif name.endswith('.json') and name != cl[0]:
                dest = safe_path(cache/'structure', name)
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(z.read(name))
    return {p.relative_to(cache).as_posix(): sha256(p) for p in cache.rglob('*') if p.is_file()}


def result_row(session, record, args):
    data = api(session,'GET',f"/extract-results/batch/{record['batch_id']}",args)
    rows = data.get('extract_result', [])
    row = next((x for x in rows if x.get('file_name') == record['name']), None)
    if not row: raise NeedsDecision('远端批次缺少预期文件；保留批次')
    return row


def process_chunk(session, part, index, state, state_path, dest, args):
    a,b,path = part
    key = f'{a+1}-{b}'
    cache = safe_path(dest, f'_raw/chunk_{index:04d}')
    rec = state.setdefault('records',{}).setdefault(key,{})
    if rec.get('status') == 'cached' and validate_hashes(cache, rec.get('hashes')):
        LOG.info('复用已核查分块 %s', key)
        return cache
    if rec.get('status') == 'cached':
        LOG.warning('分块缓存缺失/损坏，先查询远端 %s', key)
        rec['status'] = 'downloaded_missing'
    if rec.get('status') == 'allocating' and not rec.get('batch_id'):
        raise NeedsDecision('申请上传 URL 结果不确定；禁止自动重新提交')
    if rec.get('status') == 'failed':
        if not args.retry_failed:
            raise NeedsDecision('分块已失败；检查后可用 --retry-failed 有限重新提交')
        if rec.get('attempts',1) >= args.max_submissions:
            raise NeedsDecision('已达分块提交次数上限')
        attempts = rec.get('attempts',1)
        rec.clear(); rec['attempts'] = attempts
    upload_url = None
    if not rec.get('batch_id'):
        check_pause(args)
        name = f"source_{state['sha256'][:16]}_p{a+1}-{b}.pdf"
        rec.update(status='allocating',name=name,attempts=rec.get('attempts',0)+1)
        save_json(state_path,state)  # Persist uncertainty before any mutating request.
        payload={'files':[{'name':name,'data_id':name[:-4],'is_ocr':True}],
                 'model_version':args.model,'language':args.language,
                 'enable_formula':True,'enable_table':True}
        try:
            data = api(session,'POST','/file-urls/batch',args,json=payload)
        except AuthenticationError:
            # A definite authorization rejection did not allocate a task.
            rec.update(status='not_submitted',attempts=rec['attempts']-1)
            save_json(state_path,state)
            raise
        urls = data.get('file_urls',[])
        if not data.get('batch_id') or len(urls)!=1:
            raise NeedsDecision('申请结果不完整；禁止自动重传')
        upload_url=urls[0]
        rec.update(batch_id=data['batch_id'],status='allocated')
        save_json(state_path,state)  # Signed URLs never enter saved state.
        check_pause(args)
        rec['status']='upload_uncertain'; save_json(state_path,state)
        try:
            with path.open('rb') as f:
                r=session.put(upload_url,data=f,timeout=(30,1800))
            if not r.ok: raise requests.ConnectionError(f'上传 HTTP {r.status_code}')
        except requests.RequestException:
            # Poll once before handing off an ambiguous upload.
            row=result_row(session,rec,args)
            if row.get('state') == 'waiting-file':
                raise NeedsDecision('上传结果不确定；远端仍等待文件，需判断，不自动重复 PUT') from None
        rec['status']='uploaded'; save_json(state_path,state)
    deadline=time.monotonic()+args.max_wait_minutes*60
    while time.monotonic()<deadline:
        check_pause(args)
        row=result_row(session,rec,args)
        remote=row.get('state')
        LOG.info('chunk %s remote=%s',key,remote)
        emit('progress',chunk=key,state=remote)
        if remote=='done':
            url=row.get('full_zip_url')
            if not url: raise NeedsDecision('远端完成但无 ZIP 链接')
            tmp=safe_path(dest,f'_tmp/chunk_{index:04d}.zip')
            tmp.parent.mkdir(parents=True,exist_ok=True)
            download(session,url,tmp,args)
            hashes=unpack(tmp,cache)
            rec.update(status='cached',hashes=hashes)
            save_json(state_path,state)
            return cache
        if remote=='failed':
            rec.update(status='failed',error=safe_message(row.get('err_msg','解析失败')))
            save_json(state_path,state)
            raise NeedsDecision(rec['error'])
        if remote=='waiting-file' and rec['status'] in ('allocated','upload_uncertain','downloaded_missing'):
            raise NeedsDecision('远端仍等待文件；上传链接不保存在共享状态，需判断后重新配置')
        if remote not in ('waiting-file','pending','running','converting'):
            raise NeedsDecision('未知远端状态：'+safe_message(remote))
        wait(args.poll_seconds,args)
    raise NeedsDecision('轮询超时；已保存 batch_id，续跑将先查询已有任务')


def label_value(text):
    text = str(text).strip()
    m=re.fullmatch(r'(?:第\s*)?[—–\-\s]*([0-9]{1,6}|[ivxlcdmIVXLCDM]{1,12})[—–\-\s]*(?:[页頁])?',text)
    if not m: return None
    label=m.group(1)
    if label.isdigit(): return label,int(label),'arabic'
    values={'i':1,'v':5,'x':10,'l':50,'c':100,'d':500,'m':1000}
    low=label.lower()
    if not re.fullmatch(r'm{0,3}(cm|cd|d?c{0,3})(xc|xl|l?x{0,3})(ix|iv|v?i{0,3})',low):
        return None
    value=sum(-values[c] if i+1<len(low) and values[c]<values[low[i+1]] else values[c]
              for i,c in enumerate(low))
    return label,value,'roman'


def manual_map(source, total):
    path=source.with_suffix(source.suffix+'.pages.csv')
    result={}
    if not path.exists(): return result
    with path.open(encoding='utf-8-sig',newline='') as f:
        for row in csv.DictReader(f):
            page=int(row['pdf_page'])
            status=row['status'].strip()
            label=row.get('printed_label','').strip()
            evidence=row.get('evidence','').strip()
            if page in result or not 1<=page<=total or status not in ('observed','inferred','absent') or not evidence:
                raise NeedsDecision('人工页码 CSV 含重复/越界/无证据条目')
            if (status!='absent' and not label) or len(label)>32 or '\n' in label or '\r' in label:
                raise NeedsDecision('人工页码标签无效')
            if status=='absent' and label:
                raise NeedsDecision('无页码条目不能同时有标签')
            result[page]={'printed_label':label or None,'status':status,'evidence':evidence,
                          'method':'provided_map'}
    return result


def printed_pages(source,total,args,tmp):
    provided=manual_map(source,total)
    if args.no_printed_pages:
        if provided: raise NeedsDecision('无印刷页码选项与人工映射冲突')
        return [{'pdf_page':n,'printed_label':None,'status':'absent',
                 'method':'user_declared','evidence':'用户通过 --no-printed-pages 确认全书无印刷页码'}
                for n in range(1,total+1)]
    candidates=[]
    can_ocr=bool(shutil.which('tesseract'))
    if not can_ocr: LOG.warning('Tesseract 未安装：扫描页印刷页码保持待确认，文字层仍可识别')
    with fitz.open(source) as doc:
        for index,page in enumerate(doc):
            check_pause(args)
            h=page.rect.height
            options={}
            for block in page.get_text('blocks'):
                x0,y0,x1,y1,text,*_=block
                if y1<=h*.15 or y0>=h*.85:
                    v=label_value(text)
                    if v: options[(v[1],v[2])]={'label':v[0],'value':v[1],'kind':v[2],
                                                  'method':'text_margin','evidence':f'text={text.strip()!r}; bbox={[x0,y0,x1,y1]}'}
            if not options and can_ocr:
                tmp.mkdir(parents=True,exist_ok=True)
                # Render the rotation-corrected page, then crop margins on the pixmap.
                pix=page.get_pixmap(matrix=fitz.Matrix(2.5,2.5),alpha=False)
                from PIL import Image
                im=Image.frombytes('RGB',[pix.width,pix.height],pix.samples)
                for side,box in [('top',(0,0,pix.width,int(pix.height*.15))),
                                 ('bottom',(0,int(pix.height*.85),pix.width,pix.height))]:
                    png=tmp/f'page_margin_{index+1}_{side}.png'
                    im.crop(box).save(png)
                    try:
                        r=subprocess.run(['tesseract',str(png),'stdout','-l','eng','--psm','11'],
                                         capture_output=True,text=True,timeout=45,check=True)
                        for line in r.stdout.splitlines():
                            v=label_value(line)
                            if v: options[(v[1],v[2])]={'label':v[0],'value':v[1],'kind':v[2],
                                                          'method':'tesseract_margin','evidence':f'{side} margin OCR={line.strip()!r}'}
                    except (subprocess.SubprocessError,OSError) as exc:
                        LOG.warning('页码 OCR page %s: %s',index+1,safe_message(exc))
                    finally:
                        png.unlink(missing_ok=True)
            candidates.append(list(options.values()))
            emit('progress',stage='printed_pages',page=index+1,total=total)
    result=[]
    for i,options in enumerate(candidates):
        item={'pdf_page':i+1,'printed_label':None,'status':'unknown','method':'unconfirmed',
              'evidence':'页边页码未识别或缺少连续编号证据','candidates':options}
        # Accept observed tokens only with a unique three-page consecutive sequence.
        supported=[]
        for c in options:
            for a in range(max(0,i-2),min(i,total-3)+1):
                matches=[]
                for k in range(a,a+3):
                    m=[x for x in candidates[k] if x['kind']==c['kind'] and x['value']==c['value']+k-i]
                    if len(m)!=1: break
                    matches.append(m[0])
                if len(matches)==3:
                    supported.append(c); break
        if len(supported)==1:
            c=supported[0]
            item.update(printed_label=c['label'],status='detected',method=c['method'],
                        evidence=c['evidence']+'; unique consecutive three-page sequence')
        item.update(provided.get(i+1,{}))
        result.append(item)
    return result


def field_text(value):
    if isinstance(value,str): return value
    if isinstance(value,list) and all(isinstance(x,str) for x in value): return '\n'.join(value)
    if value is None: return ''
    raise NeedsDecision('内容字段格式不兼容；保留原始结构供判断')


def block_md(block,prefix,cache):
    kind=block.get('type')
    if kind in ('page_number','header','footer','discarded'): return ''
    if kind not in ('text','title','list','table','image','equation','code'):
        raise NeedsDecision('不支持的内容类型：'+safe_message(kind))
    bits=[]
    body=field_text(block.get('text') or block.get('table_body') or block.get('content') or block.get('list_items'))
    def image_ref(path):
        local=safe_path(cache,path)
        if not local.is_file(): raise NeedsDecision('图片引用缺失')
        return prefix+'/'+PurePosixPath(str(path).replace('\\','/')).as_posix()
    body=re.sub(r'(!\[[^\]]*\]\()([^\s)]+)(\))',lambda m:m[1]+image_ref(m[2])+m[3],body)
    if body.strip():
        level=block.get('text_level')
        if level is not None:
            if not isinstance(level,int): raise NeedsDecision('标题层级无效')
            body='#'*min(6,max(1,level))+' '+body
        if kind=='equation' and not body.strip().startswith('$$'): body='$$\n'+body+'\n$$'
        bits.append(body.strip())
    if block.get('img_path'):
        bits.append(f"![{kind}]({image_ref(block['img_path'])})")
    for name in ('image_caption','image_footnote','table_caption','table_footnote'):
        value=field_text(block.get(name))
        if value.strip(): bits.append(value.strip())
    if not bits: raise NeedsDecision('内容块无法转换，禁止静默遗漏')
    return '\n\n'.join(bits)


def marker(item):
    base=f"**【PDF 第 {item['pdf_page']} 页"
    if item['printed_label']:
        q='（推算）' if item['status']=='inferred' else ''
        base+=f"｜原书第 {item['printed_label']} 页{q}"
    elif item['status']!='absent': base+='｜原书页码未识别／待确认'
    return base+'】**'


def build_document(dest,spans,caches,page_map,state,args):
    parts=[]; all_blocks=[]; outputs=[]
    for index,((a,b,_),cache) in enumerate(zip(spans,caches),1):
        check_pause(args)
        rows=read_json(cache/'content_list.json')
        content={n:[] for n in range(a+1,b+1)}
        last=-1
        prefix=f'images/chunk_{index:04d}'
        for row in rows:
            if not isinstance(row,dict) or not isinstance(row.get('page_idx'),int) or isinstance(row.get('page_idx'),bool):
                raise NeedsDecision('V1 内容块必须有整数 page_idx')
            local=row['page_idx']
            if not 0<=local<b-a or local<last:
                raise NeedsDecision('内容块页码范围/顺序错误')
            last=local
            n=a+local+1
            all_blocks.append({**row,'pdf_page':n,'source_chunk':index})
            rendered=block_md(row,prefix,cache)
            if rendered: content[n].append(rendered)
        for image in (cache/'images').rglob('*') if (cache/'images').exists() else []:
            if image.is_file():
                rel=prefix+'/'+image.relative_to(cache).as_posix()
                target=safe_path(dest,rel); target.parent.mkdir(parents=True,exist_ok=True)
                shutil.copyfile(image,target); outputs.append(target)
        for n in range(a+1,b+1):
            page_map[n-1]['has_ocr_blocks']=bool(content[n])
            page_map[n-1]['content_status']='extracted' if content[n] else 'no_blocks_needs_review'
            parts.append(marker(page_map[n-1])+'\n\n'+'\n\n'.join(content[n]))
    md='\n\n'.join(parts).rstrip()+'\n'
    if [int(n) for n in re.findall(r'\*\*【PDF 第 (\d+) 页',md)]!=list(range(1,len(page_map)+1)):
        raise NeedsDecision('最终物理页码覆盖错误')
    for image in re.findall(r'!\[[^\]]*\]\(([^\s)]+)\)',md):
        if not safe_path(dest,image).is_file(): raise NeedsDecision('最终图片链接缺失')
    md_path=dest/'document.md'
    md_path.with_suffix('.md.tmp').write_text(md,encoding='utf-8')
    md_path.with_suffix('.md.tmp').replace(md_path)
    save_json(dest/'page_map.json',{'source_sha256':state['sha256'],'total_pdf_pages':len(page_map),'pages':page_map})
    tmp=dest/'page_blocks.jsonl.tmp'
    tmp.write_text(''.join(json.dumps(x,ensure_ascii=False)+'\n' for x in all_blocks),encoding='utf-8')
    tmp.replace(dest/'page_blocks.jsonl')
    outputs.extend([md_path,dest/'page_map.json',dest/'page_blocks.jsonl'])
    hashes={p.relative_to(dest).as_posix():sha256(p) for p in outputs}
    # Include all retained raw data in final validation; do not trust just the Markdown.
    for cache in caches:
        for p in cache.rglob('*'):
            if p.is_file(): hashes[p.relative_to(dest).as_posix()]=sha256(p)
    return hashes


def cleanup(dest):
    # Only these two script-owned temporary directories may be removed.
    for name in ('_tmp','_working'):
        path=safe_path(dest,name)
        if path.is_symlink(): raise NeedsDecision('临时目录为符号链接；不清理')
        if path.exists(): shutil.rmtree(path)


def markdown_name(source):
    return source.stem + '.md'


def name_key(source):
    return unicodedata.normalize('NFC',markdown_name(source)).casefold()


def collection_target(source,out):
    folder=safe_path(out,'Markdown')
    target=safe_path(folder,markdown_name(source))
    index_path=folder/'_index.json'
    index=read_json(index_path) if index_path.exists() else {}
    entry=index.get(name_key(source))
    if entry and entry.get('source')!=str(source.resolve()):
        raise NeedsDecision('不同来源 PDF 同名：'+source.name+'；请先重命名输入 PDF，不能覆盖已有 Markdown')
    existing=[p for p in folder.glob('*.md') if unicodedata.normalize('NFC',p.name).casefold()==name_key(source)] if folder.exists() else []
    for path in existing:
        if not entry or path.name!=markdown_name(source):
            raise NeedsDecision('集中目录存在同名 Markdown，无法确认归属：'+path.name)
        accepted={entry.get('markdown_sha256')}
        if entry.get('publishing'): accepted.add(entry.get('previous_markdown_sha256'))
        if entry.get('markdown_sha256') and sha256(path) not in accepted:
            raise NeedsDecision('集中 Markdown 已被修改；请先保留或移走该文件：'+path.name)
    return folder,target,index_path,index


def collect_markdown(source,out,dest):
    folder,target,index_path,index=collection_target(source,out)
    folder.mkdir(parents=True,exist_ok=True)
    sid=dest.name
    image_prefix='images/'+sid+'/'
    md=(dest/'document.md').read_text(encoding='utf-8')
    md=re.sub(r'(!\[[^\]]*\]\()([^\s)]+)(\))',
              lambda m:m[1]+image_prefix+m[2]+m[3],md)
    assets={}
    for image in (dest/'images').rglob('*') if (dest/'images').exists() else []:
        if image.is_file():
            rel=image_prefix+image.relative_to(dest).as_posix()
            copy=safe_path(folder,rel)
            copy.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(image,copy)
            assets[rel]=sha256(copy)
    for rel in re.findall(r'!\[[^\]]*\]\(([^\s)]+)\)',md):
        if not safe_path(folder,rel).is_file():
            raise NeedsDecision('集中 Markdown 的图片引用缺失')
    entry={'source':str(source.resolve()),'source_sha256':sha256(source),
           'markdown':target.name,'markdown_sha256':hashlib.sha256(md.encode('utf-8')).hexdigest(),
           'assets':assets,'publishing':True,
           'previous_markdown_sha256':index.get(name_key(source),{}).get('markdown_sha256')}
    # Record ownership before atomic publication, so interrupted first exports can resume.
    index[name_key(source)]=entry
    save_json(index_path,index)
    tmp=target.with_name(target.name+'.tmp')
    tmp.write_text(md,encoding='utf-8'); tmp.replace(target)
    if sha256(target)!=entry['markdown_sha256'] or (assets and not validate_hashes(folder,assets)):
        raise NeedsDecision('集中输出校验失败')
    entry.pop('publishing'); entry.pop('previous_markdown_sha256')
    save_json(index_path,index)
    LOG.info('集中整理 %s -> %s',source,target)
    return str(target)


def process_pdf(source,out,session,args):
    check_pause(args)
    collection_target(source,out)  # Detect filename conflicts before any upload.
    digest=sha256(source)
    loc=hashlib.sha256(str(source).encode()).hexdigest()[:8]
    sid=re.sub(r'[^\w.-]+','_',source.stem)[:60]+'__'+digest[:12]+'_'+loc
    dest=safe_path(out,sid)
    dest.mkdir(parents=True,exist_ok=True)
    state_path=dest/'_state.json'
    state=read_json(state_path) if state_path.exists() else {'sha256':digest,'source':str(source)}
    reader=PdfReader(source,strict=False)
    if reader.is_encrypted: raise NeedsDecision('加密 PDF 需要先提供可读取副本')
    total=len(reader.pages)
    csv_path=source.with_suffix(source.suffix+'.pages.csv')
    config={'model':args.model,'language':args.language,'no_printed_pages':args.no_printed_pages,
            'page_map_sha256':sha256(csv_path) if csv_path.exists() else None}
    if state.get('sha256')!=digest or (state.get('config') and state['config']!=config):
        raise NeedsDecision('源文件或配置已改变；使用新输出目录或先判断旧任务')
    state['config']=config
    final=dest/'manifest.json'
    if final.exists():
        manifest=read_json(final)
        if manifest.get('status')=='complete' and validate_hashes(dest,manifest.get('hashes')):
            markdown=collect_markdown(source,out,dest)
            cleanup(dest)
            return {'source':str(source),'status':'complete','reused':True,'output':str(dest),
                    'markdown':markdown,
                    **manifest['counts']}
        LOG.warning('最终产物缺失/损坏；核查成功分块并重建')
        final.unlink()  # Never leave a stale completion marker after a validation failure.
    state['status']='processing'; save_json(state_path,state)
    spans=chunks(reader,source,safe_path(dest,'_working'))
    state['chunks']=[{'start':a+1,'end':b} for a,b,_ in spans]; save_json(state_path,state)
    caches=[]
    for i,part in enumerate(spans,1):
        emit('progress',source=source.name,chunk=i,total_chunks=len(spans))
        caches.append(process_chunk(session,part,i,state,state_path,dest,args))
    page_map=printed_pages(source,total,args,safe_path(dest,'_tmp'))
    hashes=build_document(dest,spans,caches,page_map,state,args)
    if sha256(source)!=digest: raise NeedsDecision('处理期间源 PDF 改变，未发布完成状态')
    counts={'pages':total,'printed_detected':sum(p['status']=='detected' for p in page_map),
            'printed_provided':sum(p['status']=='observed' for p in page_map),
            'printed_inferred':sum(p['status']=='inferred' for p in page_map),
            'printed_unknown':sum(p['status']=='unknown' for p in page_map),
            'no_content_pages':sum(not p['has_ocr_blocks'] for p in page_map)}
    manifest={'status':'complete','source_sha256':digest,'counts':counts,'hashes':hashes,
              'chunks':state['chunks'],'verification':'automatic; printed labels not manually verified'}
    save_json(final,manifest)
    state['status']='complete'; save_json(state_path,state)
    markdown=collect_markdown(source,out,dest)
    cleanup(dest)
    return {'source':str(source),'status':'complete','reused':False,'output':str(dest),
            'markdown':markdown,**counts}


@contextmanager
def output_lock(out):
    path=out/'_run.lock'
    if path.exists():
        old=read_json(path)
        if old.get('host')!=socket.gethostname(): raise NeedsDecision('输出目录有另一台电脑的运行锁')
        try: os.kill(int(old['pid']),0)
        except ProcessLookupError: path.unlink()
        except (PermissionError,ValueError,KeyError): raise NeedsDecision('运行锁需检查')
        else: raise NeedsDecision('该输出目录已有运行中的脚本')
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w') as f: json.dump({'pid':os.getpid(),'host':socket.gethostname()},f)
    try: yield
    finally: path.unlink(missing_ok=True)


def parse_args(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--input',type=Path)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--dry-run',action='store_true')
    p.add_argument('--status',action='store_true',help='read the compact saved status without starting OCR')
    p.add_argument('--no-recursive',action='store_true')
    p.add_argument('--exclude',type=Path,action='append',default=[])
    p.add_argument('--no-printed-pages',action='store_true',help='only when the user confirms no printed page numbers')
    p.add_argument('--retry-failed',action='store_true')
    p.add_argument('--max-submissions',type=int,default=2)
    p.add_argument('--network-retries',type=int,default=2)
    p.add_argument('--poll-seconds',type=float,default=15)
    p.add_argument('--max-wait-minutes',type=float,default=120)
    p.add_argument('--model',choices=('vlm','pipeline'),default='vlm')
    p.add_argument('--language',default='ch')
    p.add_argument('--pause-file',type=Path,default=Path.home()/'.mineru_pause')
    a=p.parse_args(argv)
    if not a.status and not a.input: p.error('--input is required')
    if not 0<=a.network_retries<=5 or not 1<=a.max_submissions<=3 or a.poll_seconds<=0 or a.max_wait_minutes<=0:
        p.error('重试/轮询参数越界')
    a.output=a.output.expanduser().resolve()
    a.pause_file=a.pause_file.expanduser().resolve()
    return a


def compact_report(report):
    results=report.get('results',[])
    failures=[x for x in results if x['status']!='complete']
    return {'status':report.get('status'),'expected':report.get('expected'),
            'completed':sum(x['status']=='complete' for x in results),
            'failed_or_paused':len(failures),
            'unprocessed':len(report['unprocessed']) if 'unprocessed' in report else max(0,report.get('expected',0)-len(results)),
            'printed_unknown':sum(x.get('printed_unknown',0) for x in results),
            'no_content_pages':sum(x.get('no_content_pages',0) for x in results),
            'failure_summary':failures[:3],'current':report.get('current'),
            'markdown_dir':report.get('markdown_dir'),
            'log':report.get('log'),'report':report.get('report')}


def main(argv=None):
    global TOKEN
    args=parse_args(argv)
    if args.status:
        status=args.output/'_status.json'
        print(json.dumps(compact_report(read_json(status)) if status.exists() else {'status':'no_saved_run'},ensure_ascii=False))
        return 0
    sources=inventory(args.input.expanduser().resolve(),args.output,not args.no_recursive,
                      [p.expanduser().resolve() for p in args.exclude])
    if not sources:
        print(json.dumps({'event':'failure','reason':'没有 PDF 输入'},ensure_ascii=False)); return 1
    names={}
    for source in sources:
        key=name_key(source)
        if key in names:
            print(json.dumps({'event':'failure','reason':'不同来源 PDF 同名；请先重命名输入，集中输出不能覆盖',
                              'sources':[str(names[key]),str(source)]},ensure_ascii=False)); return 1
        names[key]=source
    if args.dry_run:
        result=[]
        for source in sources:
            reader=PdfReader(source,strict=False)
            n=len(reader.pages)
            result.append({'source':str(source),'pages':n,'estimated_chunks':(n+199)//200,
                           'bytes':source.stat().st_size})
        print(json.dumps({'event':'preflight','files':result,'printed_pages_default':True,
                          'tesseract_available':bool(shutil.which('tesseract'))},ensure_ascii=False))
        return 0
    TOKEN=os.environ.get('MINERU_API_TOKEN','').strip()
    if not TOKEN:
        print(json.dumps({'event':'failure','reason':'需要环境变量 MINERU_API_TOKEN；不要把密钥写进文件'},ensure_ascii=False)); return 2
    args.output.mkdir(parents=True,exist_ok=True)
    log_path=args.output/'_run.log'
    logging.basicConfig(filename=log_path,level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s',force=True)
    # Third-party warnings go to the same log, not the AI's context.
    logging.captureWarnings(True)
    report={'status':'running','expected':len(sources),'results':[],'log':str(log_path),
            'markdown_dir':str(args.output/'Markdown'),
            'report':str(args.output/'_report.json')}
    try:
        with output_lock(args.output),requests.Session() as session:
            emit('start',force=True,files=len(sources),log=str(log_path))
            for source in sources:
                try:
                    report['current']=str(source); save_json(args.output/'_status.json',report)
                    result=process_pdf(source,args.output,session,args)
                except (Exception,KeyboardInterrupt) as exc:
                    msg=safe_message(exc) or type(exc).__name__
                    LOG.error('%s: %s (%s)',source,msg,type(exc).__name__)
                    result={'source':str(source),'status':'paused' if isinstance(exc,(Paused,KeyboardInterrupt)) else 'needs_decision',
                            'reason':msg}
                    report['results'].append(result)
                    emit('failure',force=True,**result)
                    if isinstance(exc,(AuthenticationError,Paused,KeyboardInterrupt)): break
                else:
                    report['results'].append(result)
                    emit('progress',completed=sum(x['status']=='complete' for x in report['results']),total=len(sources))
                save_json(args.output/'_status.json',report)
            report['completed']=sum(x['status']=='complete' for x in report['results'])
            report['unprocessed']=[str(s) for s in sources if str(s) not in {x['source'] for x in report['results']}]
            report['status']='complete' if report['completed']==len(sources) else 'incomplete'
            report.pop('current',None)
            save_json(args.output/'_report.json',report); save_json(args.output/'_status.json',report)
            emit('final',force=True,**compact_report(report))
            return 0 if report['status']=='complete' else 1
    except (Exception,KeyboardInterrupt) as exc:
        emit('failure',force=True,reason=safe_message(exc),report=str(args.output/'_status.json'))
        return 1


if __name__=='__main__':
    try:
        raise SystemExit(main())
    except (Exception,KeyboardInterrupt) as exc:
        print(json.dumps({'event':'failure','reason':safe_message(exc),'type':type(exc).__name__},ensure_ascii=False),flush=True)
        raise SystemExit(1)
