"""Run isolated single-frame rules and build a local, human-review comparison.

Example (run in the project's vision environment):
  python tools/review_ipscell_single_image.py --images data/ipscell \
      --out outputs_ipscell_review --fov-width-mm 3.22 --fov-height-mm 3.22
No production entrypoint, configuration, source image or hardware is modified.
"""
import argparse
from collections import Counter
from dataclasses import asdict
import hashlib
import html
import json
import os
from pathlib import Path
import platform
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import cv2
import numpy as np
from vision.experiments.single_image_rules import ExperimentConfig, analyze
from vision.vision.legacy_detect_pipeline import detect_from_gray


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')


def write_image(path, value):
    ok, data = cv2.imencode(path.suffix, value)
    if not ok:
        raise ValueError(f'Could not encode {path}')
    data.tofile(str(path))


def draw(gray, result, baseline=False):
    h, w = gray.shape
    scale = min(1., 1600 / max(h, w))
    view = cv2.cvtColor(cv2.resize(gray, (round(w*scale), round(h*scale)), interpolation=cv2.INTER_AREA), cv2.COLOR_GRAY2BGR)
    for c in result['components'] + ([] if baseline else result['dark_regions']):
        if baseline:
            color = (50,220,40) if c['is_pickable'] else (0,170,255)
        else:
            color = ((50,220,40) if c['status']=='normal_candidate'
                     else (25,40,240) if c['status']=='overgrown' else (0,170,255))
        points = np.asarray(c.get('contour_points') or [], np.float64)
        if len(points):
            pts = np.rint(points * scale).astype(np.int32)
            cv2.polylines(view, [pts], True, color, 3, cv2.LINE_AA)
            x,y = pts[np.argmin(pts[:,1])]
        else:
            x,y,bw,bh = np.rint(np.array(c['coarse_bbox'])*scale).astype(int)
            cv2.rectangle(view,(x,y),(x+bw,y+bh),color,2)
        origin=(max(4,min(int(x),view.shape[1]-85)),max(24,min(int(y),view.shape[0]-8)))
        cv2.putText(view,c['id'],origin,cv2.FONT_HERSHEY_SIMPLEX,.7,(0,0,0),5,cv2.LINE_AA)
        cv2.putText(view,c['id'],origin,cv2.FONT_HERSHEY_SIMPLEX,.7,color,2,cv2.LINE_AA)
    return view


def build_html(out, records):
    cards=[]
    status_names={'normal_candidate':'正常纹理候选','overgrown':'疑似过度生长：禁止挑取',
                  'texture_review':'纹理/形状待复核','dark_edge_review':'边缘暗区待复核','dark_review':'局部暗区待复核'}
    for record in records:
        stem=record['stem']; candidate=record['candidate']; rows=[]
        for c in candidate['components']+candidate['dark_regions']:
            key=f'{stem}:{c["id"]}'
            rows.append(f'<tr><td>{c["id"]}</td><td>{status_names[c["status"]]}</td>'
                        f'<td>{c["area_mm2"]:.3f}</td><td>{"是" if c["touch_image_border"] else "否"}</td>'
                        f'<td><select data-key="{html.escape(key)}"><option>未复核</option><option>正确</option>'
                        '<option>误检，应删除</option><option>应保留为正常克隆</option><option>应判为过度生长</option>'
                        '<option>边界需修改</option></select></td></tr>')
        if not rows: rows=['<tr><td colspan="5">无候选，请检查是否漏检。</td></tr>']
        cards.append(f'''<article id="{stem}"><h2>{stem}</h2>
<p>基线可挑取：{record['baseline_pickable']}；试验正常纹理候选：{record['counts'].get('normal_candidate',0)}。
基线 {record['baseline_seconds']:.2f}s / 试验 {record['candidate_seconds']:.2f}s（CPU 单次检测，不含读图及输出）。</p>
<div class="views">{''.join(f'<figure><a href="{stem}/{file}"><img loading="lazy" src="{stem}/{file}"></a><figcaption>{label}</figcaption></figure>' for file,label in [('input.jpg','原图预览'),('baseline.jpg','现有规则：绿=可挑取，橙框=失败候选'),('candidate.jpg','试验：绿=正常纹理候选，红=过度生长候选，橙=待复核')])}</div>
<p><a href="{html.escape(record['original_link'])}">5120 原始 BMP</a> · <a href="{stem}/candidate.json">试验 JSON</a> · <a href="{stem}/baseline.json">基线 JSON</a> · <a href="{stem}/occupancy.png">纹理密度</a> · <a href="{stem}/dark_mask.png">暗区掩膜</a> · <a href="{stem}/low_visibility.png">低可见区</a></p>
<table><thead><tr><th>编号</th><th>试验判定</th><th>面积 mm²</th><th>图像截断</th><th>你的复核</th></tr></thead><tbody>{''.join(rows)}</tbody></table>
<label>漏检位置、边界或分类意见<textarea data-key="{stem}:notes" placeholder="例如：左上漏检；C02 是碎屑；D01 应为孔边阴影"></textarea></label>
</article>''')
    page='''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>iPSC 单图规则试验复核</title>
<style>body{font:16px/1.6 system-ui,sans-serif;margin:24px;color:#172b3a;background:#f3f5f8}article,header{background:white;padding:22px;margin:20px 0;border-radius:12px}h1{margin-top:0}.views{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}figure{margin:0}img{width:100%;display:block}figcaption{font-size:14px}table{border-collapse:collapse;width:100%}th,td{text-align:left;padding:8px;border-bottom:1px solid #ddd}textarea{display:block;width:98%;height:75px}button{padding:10px 18px;cursor:pointer}nav a{margin-right:15px}select{padding:5px}@media(max-width:1000px){.views{grid-template-columns:1fr}}</style>
<header><h1>iPSC 单张图片：基线与试验对照</h1><p>仅离线试验，未接入扫描或挑取。绿色代表待你验证的正常纹理候选，所有试验记录 <code>is_pickable=false</code>。
红色为规则推测的过度生长区域，不是人工真值；橙色需要复核。C 编号为纹理实例，D 编号为暗区子区域，二者可重叠，不应相加作克隆总数。</p>
<p>本轮按完整 3.22×3.22 mm 视野测试；如有裁剪或不同标定，应重新运行。没有人工轮廓标注，不报告精确率/召回率。
点击图片可放大。填写意见后点击导出，保存的 JSON 可用于下一轮修改；刷新页面前请导出。</p>
<button id="export">导出复核意见 JSON</button><nav>'''+''.join(f'<a href="#{r["stem"]}">{r["stem"].split("_")[0]}</a>' for r in records)+'''</nav></header>'''+''.join(cards)+'''
<script>document.getElementById('export').onclick=()=>{const review={schema_version:1,experiment:'experimental_single_image_v1',created_at:new Date().toISOString(),responses:{}};document.querySelectorAll('[data-key]').forEach(e=>review.responses[e.dataset.key]=e.value);const url=URL.createObjectURL(new Blob([JSON.stringify(review,null,2)],{type:'application/json'}));const a=document.createElement('a');a.href=url;a.download='ipscell_review.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);};</script></html>'''
    (out/'index.html').write_text(page,encoding='utf-8')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--images',type=Path,required=True)
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--fov-width-mm',type=float,required=True)
    parser.add_argument('--fov-height-mm',type=float,required=True)
    args=parser.parse_args()
    files=sorted(args.images.glob('*.bmp'))
    if not files: raise ValueError('No BMP inputs')
    args.out.mkdir(parents=True,exist_ok=False)
    cv2.setNumThreads(1)
    cv2.setRNGSeed(0)
    records=[]
    for path in files:
        payload=path.read_bytes();digest=hashlib.sha256(payload).hexdigest()
        gray=cv2.imdecode(np.frombuffer(payload,np.uint8),cv2.IMREAD_GRAYSCALE)
        if gray is None: raise ValueError(f'Cannot decode {path}')
        t=time.perf_counter();baseline=detect_from_gray(gray,texture_backend='cpu');bt=time.perf_counter()-t
        t=time.perf_counter();candidate,maps=analyze(gray,fov_mm=(args.fov_width_mm,args.fov_height_mm));ct=time.perf_counter()-t
        folder=args.out/path.stem;folder.mkdir()
        write_json(folder/'baseline.json',baseline);write_json(folder/'candidate.json',candidate)
        preview=cv2.resize(gray,(1600,1600),interpolation=cv2.INTER_AREA) if gray.shape[0]==gray.shape[1] else cv2.cvtColor(draw(gray,{'components':[]},True),cv2.COLOR_BGR2GRAY)
        write_image(folder/'input.jpg',preview)
        write_image(folder/'baseline.jpg',draw(gray,baseline,True))
        write_image(folder/'candidate.jpg',draw(gray,candidate))
        for name in ('support','seed','dark_mask','low_visibility','accepted_mask'):
            write_image(folder/(name+'.png'),maps[name])
        write_image(folder/'occupancy.png',np.uint8(np.clip(maps['occupancy'],0,1)*255))
        record=dict(stem=path.stem,input_sha256=digest,input_path=str(path.resolve()),
                    original_link=Path(os.path.relpath(path.resolve(),args.out.resolve())).as_posix(),
                    baseline_seconds=bt,candidate_seconds=ct,
                    baseline_pickable=sum(c['is_pickable'] for c in baseline['components']),
                    counts=dict(Counter(c['status'] for c in candidate['components'])),candidate=candidate)
        records.append(record)
        print(f'{path.name}: baseline={record["baseline_pickable"]}, experiment={record["counts"]}, {ct:.2f}s',flush=True)
    manifest=dict(algorithm='experimental_single_image_v1',production_enabled=False,
                  python=sys.version,platform=platform.platform(),opencv=cv2.__version__,numpy=np.__version__,
                  fov_mm=[args.fov_width_mm,args.fov_height_mm],config=asdict(ExperimentConfig()),
                  images=[{k:v for k,v in r.items() if k!='candidate'} for r in records],
                  code_sha256={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in
                               [ROOT/'vision/experiments/single_image_rules.py',Path(__file__).resolve(),
                                *sorted((ROOT/'vision/vision').glob('*.py'))]})
    write_json(args.out/'manifest.json',manifest)
    write_json(args.out/'review_template.json',{'responses':{f'{r["stem"]}:notes':'' for r in records}})
    build_html(args.out,records)
    print(args.out/'index.html',flush=True)


if __name__=='__main__':
    main()
