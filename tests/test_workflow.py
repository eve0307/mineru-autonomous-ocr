import importlib.util
import io
import json
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import fitz
import requests
from PIL import Image, ImageDraw, ImageFont

MODULE=Path(__file__).resolve().parents[1]/'scripts/run_ocr.py'
spec=importlib.util.spec_from_file_location('ocr',MODULE)
ocr=importlib.util.module_from_spec(spec); spec.loader.exec_module(ocr)


def pdf(path,n,numbers=False):
    with fitz.open() as doc:
        for i in range(n):
            page=doc.new_page(width=300,height=400)
            if numbers:
                page.insert_text((145,385),str(i+11),fontsize=12)
            page.insert_text((30,100),'Example body',fontsize=12)
        doc.save(path)


def result_zip(n=1,image=False,bad_idx=False,kind='text'):
    rows=[{'type':kind,'text':f'Body {i}','page_idx':i if not bad_idx else n} for i in range(n)]
    if image:
        rows.append({'type':'image','img_path':'images/a.png','image_caption':['Caption'],'page_idx':n-1})
    buf=io.BytesIO()
    with zipfile.ZipFile(buf,'w') as z:
        z.writestr('bundle/full.md','Original Markdown')
        z.writestr('bundle/test_content_list.json',json.dumps(rows))
        z.writestr('bundle/layout.json','{}')
        if image: z.writestr('bundle/images/a.png',b'example image bytes')
    return buf.getvalue()

class Response:
    def __init__(self,obj=None,data=b'',status=200):
        self.obj=obj; self.data=data; self.status_code=status; self.ok=status<400
    def json(self): return self.obj
    def iter_content(self,*a): yield self.data
    def __enter__(self): return self
    def __exit__(self,*a): pass

class Session:
    def __init__(self,payload=None,fail_second=False,uncertain=False):
        self.payload=payload; self.fail_second=fail_second; self.uncertain=uncertain
        self.posts=[]; self.puts=[]; self.polls=[]; self.names={}
    def __enter__(self): return self
    def __exit__(self,*a): pass
    def request(self,method,url,**kw):
        if method=='POST':
            self.posts.append(kw['json'])
            if self.uncertain: raise requests.ConnectionError('https://secret.example/?token=secret')
            batch=str(len(self.posts)); self.names[batch]=kw['json']['files'][0]['name']
            return Response({'code':0,'data':{'batch_id':batch,'file_urls':['https://upload.example/signed-secret']}})
        batch=url.rsplit('/',1)[-1]; self.polls.append(batch)
        state='failed' if self.fail_second and batch=='2' else 'done'
        return Response({'code':0,'data':{'extract_result':[{'file_name':self.names[batch],
                          'state':state,'full_zip_url':'https://download.example/'+batch,'err_msg':'simulated failure'}]}})
    def put(self,url,**kw): self.puts.append(url); return Response()
    def get(self,url,**kw):
        batch=url.rsplit('/',1)[-1]
        if self.payload: data=self.payload
        else:
            name=self.names[batch]
            a,b=map(int,name.rsplit('_p',1)[1][:-4].split('-'))
            data=result_zip(b-a+1,image=True)
        return Response(data=data)

class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.root=Path(self.temp.name).resolve()
        self.source=self.root/'book.pdf'; self.out=self.root/'out'; self.out.mkdir()
        self.args=ocr.parse_args(['--input',str(self.source),'--output',str(self.out),
                                  '--pause-file',str(self.root/'pause'),'--network-retries','0',
                                  '--poll-seconds','.001'])
        ocr.TOKEN='test-private-token'
        self.ocr_patch=patch.object(ocr.shutil,'which',return_value=None)
        self.ocr_patch.start(); self.addCleanup(self.ocr_patch.stop)
    def tearDown(self): self.temp.cleanup()
    def run_pdf(self,session,n=1):
        if not self.source.exists(): pdf(self.source,n)
        return ocr.process_pdf(self.source,self.out,session,self.args)
    def test_split_200_201_401(self):
        for n,expected in [(200,[(0,200)]),(201,[(0,200),(200,201)]),(401,[(0,200),(200,400),(400,401)])]:
            path=self.root/f'{n}.pdf'; pdf(path,n)
            spans=ocr.chunks(ocr.PdfReader(path),path,self.root/f'work{n}')
            self.assertEqual([(a,b) for a,b,p in spans],expected)
            self.assertEqual(sum(len(ocr.PdfReader(p).pages) for a,b,p in spans),n)
    def test_oversize_one_page(self):
        pdf(self.source,1)
        with patch.object(ocr,'LIMIT',1), self.assertRaises(ocr.NeedsDecision):
            ocr.chunks(ocr.PdfReader(self.source),self.source,self.root/'tmp')
    def test_full_api_images_cleanup_and_resume(self):
        s=Session(); r=self.run_pdf(s,3); dest=Path(r['output'])
        self.assertEqual(r['status'],'complete'); self.assertEqual(len(s.posts),1)
        self.assertFalse((dest/'_tmp').exists()); self.assertFalse((dest/'_working').exists())
        self.assertTrue((dest/'_raw/chunk_0001/full.md').exists())
        self.assertTrue((dest/'images/chunk_0001/images/a.png').exists())
        md=(dest/'document.md').read_text()
        self.assertIn('PDF 第 3 页',md); self.assertIn('原书页码未识别／待确认',md)
        self.assertTrue(self.run_pdf(s)['reused']); self.assertEqual(len(s.posts),1)
        state=(dest/'_state.json').read_text()
        self.assertNotIn('test-private-token',state); self.assertNotIn('signed-secret',state)
    def test_failed_chunk_no_publish_and_reuse(self):
        s=Session(fail_second=True); pdf(self.source,201)
        with self.assertRaises(ocr.NeedsDecision): self.run_pdf(s)
        dest=next(self.out.iterdir())
        self.assertFalse((dest/'manifest.json').exists()); self.assertFalse((dest/'document.md').exists())
        self.args.retry_failed=True; s.fail_second=False
        r=self.run_pdf(s); self.assertEqual(r['pages'],201)
        self.assertEqual(len(s.posts),3)  # First successful chunk was not resubmitted.
    def test_uncertain_post_never_resubmitted(self):
        s=Session(uncertain=True)
        for i in range(2):
            with self.assertRaises(ocr.NeedsDecision): self.run_pdf(s)
        self.assertEqual(len(s.posts),1)
    def test_missing_final_image_rebuilds_without_upload(self):
        s=Session(); r=self.run_pdf(s); dest=Path(r['output'])
        (dest/'images/chunk_0001/images/a.png').unlink()
        self.assertFalse(self.run_pdf(s)['reused']); self.assertEqual(len(s.posts),1)
    def test_corrupt_raw_cache_downloads_existing_batch(self):
        s=Session(); r=self.run_pdf(s); dest=Path(r['output'])
        (dest/'_raw/chunk_0001/content_list.json').write_text('broken')
        self.run_pdf(s); self.assertEqual(len(s.posts),1); self.assertEqual(len(s.puts),1)
    def test_bad_page_index_no_completion(self):
        s=Session(payload=result_zip(bad_idx=True))
        with self.assertRaises(ocr.NeedsDecision): self.run_pdf(s)
        self.assertFalse(any(self.out.rglob('manifest.json')))
    def test_unsupported_content_no_completion(self):
        s=Session(payload=result_zip(kind='new_unsupported'))
        with self.assertRaises(ocr.NeedsDecision): self.run_pdf(s)
        self.assertFalse(any(self.out.rglob('manifest.json')))
    def test_same_name_is_rejected_before_second_upload(self):
        pdf(self.source,1); other=self.root/'another'; other.mkdir(); other=other/'book.pdf'
        other.write_bytes(self.source.read_bytes()); s=Session()
        self.run_pdf(s)
        with self.assertRaises(ocr.NeedsDecision): ocr.process_pdf(other,self.out,s,self.args)
        self.assertEqual(len(s.posts),1)

    def test_collected_name_and_images(self):
        self.source=self.root/'中文书名 (修订).PDF'; s=Session()
        r=self.run_pdf(s,3)
        md=Path(r['markdown'])
        self.assertEqual(md,self.out/'Markdown'/'中文书名 (修订).md')
        for rel in ocr.re.findall(r'!\[[^\]]*\]\(([^\s)]+)\)',md.read_text()):
            self.assertTrue((md.parent/rel).is_file())

    def test_all_final_markdown_in_one_folder(self):
        s=Session(); first=self.run_pdf(s)
        second=self.root/'other.pdf'; pdf(second,1)
        other=ocr.process_pdf(second,self.out,s,self.args)
        self.assertEqual(Path(first['markdown']).parent,Path(other['markdown']).parent)
        self.assertEqual({p.name for p in (self.out/'Markdown').glob('*.md')},{'book.md','other.md'})

    def test_deleted_collection_is_rebuilt_without_upload(self):
        s=Session(); r=self.run_pdf(s)
        ocr.shutil.rmtree(self.out/'Markdown')
        new=self.run_pdf(s)
        self.assertTrue(Path(new['markdown']).exists()); self.assertEqual(len(s.posts),1)

    def test_collected_image_is_repaired_without_upload(self):
        s=Session(); r=self.run_pdf(s)
        md=Path(r['markdown'])
        rel=ocr.re.findall(r'!\[[^\]]*\]\(([^\s)]+)\)',md.read_text())[0]
        (md.parent/rel).unlink(); self.run_pdf(s)
        self.assertTrue((md.parent/rel).is_file()); self.assertEqual(len(s.posts),1)

    def test_user_edited_collection_is_not_overwritten(self):
        s=Session(); r=self.run_pdf(s); md=Path(r['markdown'])
        md.write_text('User changes')
        with self.assertRaises(ocr.NeedsDecision): self.run_pdf(s)
        self.assertEqual(md.read_text(),'User changes'); self.assertEqual(len(s.posts),1)

    def test_batch_same_names_fail_before_processing(self):
        pdf(self.source,1); sub=self.root/'sub'; sub.mkdir(); pdf(sub/'BOOK.pdf',1)
        s=Session()
        with patch.object(ocr.requests,'Session',return_value=s),redirect_stdout(io.StringIO()):
            code=ocr.main(['--input',str(self.root),'--output',str(self.out),'--dry-run'])
        self.assertEqual(code,1); self.assertFalse(s.posts)
    def test_pause_stops_before_upload(self):
        self.args.pause_file.touch(); s=Session()
        with self.assertRaises(ocr.Paused): self.run_pdf(s)
        self.assertFalse(s.posts)
    def test_401_stops(self):
        class Bad(Session):
            def request(self,*a,**kw): return Response(status=401)
        with self.assertRaises(ocr.AuthenticationError): self.run_pdf(Bad())
    def test_auth_rejection_can_resume_after_credentials_fixed(self):
        class Bad(Session):
            def request(self,*a,**kw): return Response(status=401)
        with self.assertRaises(ocr.AuthenticationError): self.run_pdf(Bad())
        self.assertEqual(self.run_pdf(Session())['status'],'complete')
    def test_margin_printed_pages(self):
        pdf(self.source,4,numbers=True)
        result=ocr.printed_pages(self.source,4,self.args,self.root/'tmp')
        self.assertEqual([x['printed_label'] for x in result],['11','12','13','14'])
        self.assertTrue(all(x['status']=='detected' for x in result))
    def test_two_candidates_are_not_verified(self):
        pdf(self.source,2,numbers=True)
        result=ocr.printed_pages(self.source,2,self.args,self.root/'tmp')
        self.assertTrue(all(x['status']=='unknown' for x in result))
    def test_scanned_margin_ocr(self):
        self.ocr_patch.stop()
        if not ocr.shutil.which('tesseract'): self.skipTest('no tesseract')
        # DejaVu ships with Pillow on this machine; use a discovered system font if needed.
        font_paths=[Path('/System/Library/Fonts/Supplemental/Arial.ttf'),Path('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf')]
        font_path=next((p for p in font_paths if p.exists()),None)
        if not font_path: self.skipTest('no test font')
        with fitz.open() as doc:
            for n in range(11,14):
                image=Image.new('RGB',(900,1200),'white'); draw=ImageDraw.Draw(image)
                draw.text((430,1120),str(n),font=ImageFont.truetype(str(font_path),40),fill='black')
                data=io.BytesIO(); image.save(data,format='PNG')
                page=doc.new_page(width=300,height=400); page.insert_image(page.rect,stream=data.getvalue())
            doc.save(self.source)
        result=ocr.printed_pages(self.source,3,self.args,self.root/'tmp')
        self.assertEqual([x['printed_label'] for x in result],['11','12','13'])
        self.assertTrue(all(x['method']=='tesseract_margin' for x in result))
    def test_no_page_override(self):
        pdf(self.source,1); self.args.no_printed_pages=True
        result=ocr.printed_pages(self.source,1,self.args,self.root/'tmp')
        self.assertEqual(result[0]['status'],'absent'); self.assertNotIn('原书',ocr.marker(result[0]))
    def test_provided_map_evidence_and_inference(self):
        pdf(self.source,1)
        csv=self.source.with_suffix('.pdf.pages.csv')
        csv.write_text('pdf_page,printed_label,status,evidence\n1,iii,inferred,manual sequence\n')
        result=ocr.printed_pages(self.source,1,self.args,self.root/'tmp')
        self.assertIn('推算',ocr.marker(result[0]))
        csv.write_text('pdf_page,printed_label,status,evidence\n1,iii,observed,\n')
        with self.assertRaises(ocr.NeedsDecision): ocr.manual_map(self.source,1)
    def test_number_parser_no_words_or_invalid_roman(self):
        for text in ('will','leper','IIII','1990 12','Chapter 2'):
            self.assertIsNone(ocr.label_value(text))
        self.assertEqual(ocr.label_value('iv')[1],4)
    def test_inventory_excludes_output_and_metadata(self):
        pdf(self.source,1); pdf(self.out/'result.pdf',1); pdf(self.root/'._ghost.pdf',1)
        self.assertEqual(ocr.inventory(self.root,self.out),[self.source])
    def test_unsafe_zip(self):
        buf=io.BytesIO()
        with zipfile.ZipFile(buf,'w') as z:
            z.writestr('../evil','x'); z.writestr('full.md','x'); z.writestr('content_list.json','[]')
        path=self.root/'result.zip'; path.write_bytes(buf.getvalue())
        with self.assertRaises(ocr.NeedsDecision): ocr.unpack(path,self.root/'cache')
    def test_redaction(self):
        self.assertNotIn(ocr.TOKEN,ocr.safe_message('Bearer '+ocr.TOKEN))
        self.assertNotIn('signed-secret',ocr.safe_message('https://x/?a=signed-secret'))
    def test_configuration_change_does_not_reuse(self):
        s=Session(); self.run_pdf(s); self.args.language='en'
        with self.assertRaises(ocr.NeedsDecision): self.run_pdf(s)
    def test_cleanup_cannot_delete_source(self):
        pdf(self.source,1); dest=self.out/'doc'; dest.mkdir()
        (dest/'_tmp').symlink_to(self.root,target_is_directory=True)
        with self.assertRaises(ocr.NeedsDecision): ocr.cleanup(dest)
        self.assertTrue(self.source.exists())

    def test_main_compact_report_logs_status_and_token_exclusion(self):
        pdf(self.source,3,numbers=True); s=Session(); stdout=io.StringIO()
        with patch.dict(ocr.os.environ,{'MINERU_API_TOKEN':'test-private-token'}), \
             patch.object(ocr.requests,'Session',return_value=s), redirect_stdout(stdout):
            code=ocr.main(['--input',str(self.source),'--output',str(self.out),
                          '--pause-file',str(self.root/'pause')])
        self.assertEqual(code,0)
        final=json.loads(stdout.getvalue().splitlines()[-1])
        self.assertEqual(final['completed'],1); self.assertNotIn('results',final)
        self.assertNotIn('Body 1',stdout.getvalue())
        self.assertTrue((self.out/'_run.log').exists())
        self.assertFalse((self.out/'_run.lock').exists())
        report=json.loads((self.out/'_report.json').read_text())
        self.assertEqual(report['status'],'complete')
        for p in self.out.rglob('*'):
            if p.is_file(): self.assertNotIn(b'test-private-token',p.read_bytes())
        status=io.StringIO()
        with redirect_stdout(status): self.assertEqual(ocr.main(['--output',str(self.out),'--status']),0)
        self.assertEqual(json.loads(status.getvalue())['completed'],1)

    def test_main_auth_failure_leaves_rest_unprocessed(self):
        pdf(self.source,1); pdf(self.root/'second.pdf',1)
        class Bad(Session):
            def request(self,*a,**kw): return Response(status=401)
        with patch.dict(ocr.os.environ,{'MINERU_API_TOKEN':'test-private-token'}), \
             patch.object(ocr.requests,'Session',return_value=Bad()), redirect_stdout(io.StringIO()):
            code=ocr.main(['--input',str(self.root),'--output',str(self.out),
                          '--pause-file',str(self.root/'pause')])
        self.assertEqual(code,1)
        report=json.loads((self.out/'_report.json').read_text())
        self.assertEqual(len(report['results']),1); self.assertEqual(len(report['unprocessed']),1)

    # ---- 回归测试：真实语料中暴露的 6 类问题（见 README/交付说明）----

    def custom_zip(self,rows,extra=None):
        buf=io.BytesIO()
        with zipfile.ZipFile(buf,'w') as z:
            z.writestr('bundle/full.md','Original Markdown')
            z.writestr('bundle/test_content_list.json',json.dumps(rows))
            for name,data in (extra or {}).items(): z.writestr('bundle/'+name,data)
        return buf.getvalue()

    def test_permission_flagged_pdf_is_accepted(self):
        """仅设所有者口令/权限位的 PDF：pypdf 会标 is_encrypted，但空口令能打开，必须放行。"""
        with fitz.open() as doc:
            doc.new_page(width=300,height=400).insert_text((30,100),'Example body',fontsize=12)
            doc.save(self.source,encryption=fitz.PDF_ENCRYPT_AES_256,owner_pw='owner-secret',user_pw='')
        self.assertTrue(ocr.PdfReader(self.source,strict=False).is_encrypted)
        self.assertEqual(self.run_pdf(Session())['status'],'complete')

    def test_truly_encrypted_pdf_still_stops(self):
        """确实需要口令的 PDF 仍须交人工判断，不能因为放宽加密判定而放行。"""
        with fitz.open() as doc:
            doc.new_page(width=300,height=400).insert_text((30,100),'secret',fontsize=12)
            doc.save(self.source,encryption=fitz.PDF_ENCRYPT_AES_256,owner_pw='o',user_pw='realpassword')
        with self.assertRaises(ocr.NeedsDecision): self.run_pdf(Session())

    def test_footnote_and_reference_blocks_are_rendered(self):
        """脚注/参考文献/页边文字都是正文型内容，必须渲染，不能因此中止整本。"""
        rows=[{'type':'text','text':'正文一','page_idx':0},
              {'type':'page_footnote','text':'脚注一','page_idx':0},
              {'type':'ref_text','text':'参考文献一','page_idx':0},
              {'type':'aside_text','text':'页边一','page_idx':0}]
        r=self.run_pdf(Session(payload=self.custom_zip(rows)))
        self.assertEqual(r['status'],'complete')
        md=(Path(r['output'])/'document.md').read_text()
        for text in ('正文一','脚注一','参考文献一','页边一'): self.assertIn(text,md)

    def test_chart_image_and_caption_are_kept(self):
        """chart 块要渲染图片，且图表题注不能被静默丢弃。"""
        rows=[{'type':'chart','content':'','img_path':'images/c.png',
               'chart_caption':['图一 说明'],'chart_footnote':[],'page_idx':0}]
        r=self.run_pdf(Session(payload=self.custom_zip(rows,{'images/c.png':b'png-bytes'})))
        self.assertEqual(r['status'],'complete')
        md=(Path(r['output'])/'document.md').read_text()
        self.assertIn('图一 说明',md); self.assertIn('images/c.png',md)

    def test_blank_blocks_are_skipped_with_audit(self):
        """全字段为空的占位块跳过并留审计；[""] 这类"列表非空但元素全空"也算空。"""
        rows=[{'type':'text','text':'','page_idx':0},
              {'type':'table','table_body':'','table_caption':[''],'table_footnote':[''],'page_idx':1},
              {'type':'text','text':'有内容','page_idx':2}]
        r=self.run_pdf(Session(payload=self.custom_zip(rows)),3)
        self.assertEqual(r['status'],'complete'); self.assertEqual(r['no_content_pages'],2)
        pages=json.loads((Path(r['output'])/'page_map.json').read_text())['pages']
        self.assertEqual(sum(p.get('empty_blocks_skipped',0) for p in pages),2)
        self.assertIn('有内容',(Path(r['output'])/'document.md').read_text())

    def test_nonblank_unknown_field_still_stops(self):
        """内容藏在未适配字段里时不允许静默丢弃，必须中止交人工。"""
        rows=[{'type':'table','unknown_body':'有内容但字段名未适配','page_idx':0}]
        with self.assertRaises(ocr.NeedsDecision): self.run_pdf(Session(payload=self.custom_zip(rows)))
        self.assertFalse(any(self.out.rglob('manifest.json')))

    def test_no_force_ocr_flag_is_sent_to_api(self):
        """--no-force-ocr 时 is_ocr 必须为 False；默认仍为 True（强制 OCR）。"""
        s=Session(); self.run_pdf(s)
        self.assertTrue(s.posts[0]['files'][0]['is_ocr'])
        out2=self.root/'out2'; out2.mkdir()
        args=ocr.parse_args(['--input',str(self.source),'--output',str(out2),
                             '--pause-file',str(self.root/'pause'),'--network-retries','0',
                             '--poll-seconds','.001','--no-force-ocr'])
        s2=Session(); ocr.process_pdf(self.source,out2,s2,args)
        self.assertFalse(s2.posts[0]['files'][0]['is_ocr'])

if __name__=='__main__': unittest.main()
