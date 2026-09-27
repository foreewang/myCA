"""Compare production rules against the exact green geometry accepted by the user."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import cv2
import numpy as np
from vision.vision.detect_pipeline import process_image
from workflow.detect_api import normalize_detect_result


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--images',type=Path,default=ROOT/'data/ipscell')
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--fixture',type=Path,default=ROOT/'tests/fixtures/ipscell_v4_acceptance.json')
    args=parser.parse_args()
    fixture=json.loads(args.fixture.read_text(encoding='utf8'))
    args.out.mkdir(parents=True,exist_ok=False)
    records=[]
    cv2.setNumThreads(1)
    for case in fixture['images']:
        path=args.images/case['name']
        payload=path.read_bytes()
        if hashlib.sha256(payload).hexdigest()!=case['sha256']:
            raise AssertionError(f'Input differs from the reviewed dataset: {path}')
        gray=cv2.imdecode(np.frombuffer(payload,np.uint8),0)
        fw,fh=fixture['fov_mm']
        raw=process_image(path,mm_per_pixel={'x':fw/gray.shape[1],'y':fh/gray.shape[0]},
                          rule_algorithm=fixture.get('algorithm','single_image_rules_v2'))
        expected=case['normal_candidates'];actual=raw['components']
        assert raw['rule_config']==fixture['config'],path
        assert [c['id'] for c in actual]==[c['id'] for c in expected],path
        for a,b in zip(actual,expected):
            for key in ('id','bbox','center_pixel','area_mm2'):
                assert a[key]==b[key],(path,key)
            assert digest(a['contour_points'])==b['contour_sha256'],path
            assert a['is_pickable'] and a['is_valid_for_compensation']
        assert not any(c['is_pickable'] or c['is_valid_for_compensation'] for c in raw['review_candidates'])
        if 'review_candidates' in case:
            assert [c['id'] for c in raw['review_candidates']]==[c['id'] for c in case['review_candidates']],path
            for a,b in zip(raw['review_candidates'],case['review_candidates']):
                for key in ('id','bbox','area_mm2','status','v3_source_ids'):
                    assert a[key]==b[key],(path,key)
                if b.get('center_pixel') is not None:assert a['center_pixel']==b['center_pixel'],path
                assert digest(a['contour_points'])==b['contour_sha256'],path
        if 'stage_configs' in fixture:assert raw['rule_stage_configs']==fixture['stage_configs'],path
        normalized=normalize_detect_result(raw)
        assert normalized['clone_count']==len(expected)
        (args.out/(path.stem+'.json')).write_text(json.dumps(raw,ensure_ascii=False,indent=2),encoding='utf8')
        preview=cv2.cvtColor(cv2.resize(gray,(1024,1024),interpolation=cv2.INTER_AREA),cv2.COLOR_GRAY2BGR)
        for c in actual:
            pts=np.rint(np.asarray(c['contour_points'])*[1024/gray.shape[1],1024/gray.shape[0]]).astype(np.int32)
            cv2.polylines(preview,[pts],True,(0,255,0),2)
        cv2.imencode('.jpg',preview)[1].tofile(str(args.out/(path.stem+'.jpg')))
        record={'image':case['name'],'green_count':len(actual),'review_count':raw['review_candidate_count'],
                'exact_geometry_match':True,'input_sha256':case['sha256']}
        records.append(record)
        print(record,flush=True)
    report={'algorithm':fixture.get('algorithm','single_image_rules_v2'),'all_passed':True,'images':records,
            'green_total':sum(r['green_count'] for r in records),
            'criterion':'Exact accepted IDs, centers, bboxes, areas and contour hashes; all non-green records excluded.'}
    (args.out/'verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
    print('Verified',report['green_total'],'accepted green candidates.',flush=True)


if __name__=='__main__':
    main()
