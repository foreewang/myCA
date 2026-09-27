"""Image-only V4 experiment. Feedback coordinates never enter inference.

Physical-scale neck removal, bounded frame-edge notch completion, and weak
texture envelopes for adjacent interior review fragments. No picking enabled.
"""
from copy import deepcopy
from dataclasses import asdict,dataclass
import math
import cv2
import numpy as np
from .single_image_segment import segment_single_image
from .single_image_refine_v2 import refine_evidence,_body,_distance,_geometry,_kernel
from .single_image_refine_v3 import revise as revise_v3,_largest


@dataclass(frozen=True)
class RevisionConfig:
    bright_mean_gray: float=205.
    normal_fine_ratio: float=.37
    normal_open_um: float=110.
    notch_max_depth_um: float=350.
    notch_min_area_mm2: float=.01
    notch_support: float=.35
    merge_gap_um: float=350.
    merge_bridge_support: float=.35
    fragment_max_area_mm2: float=.65
    envelope_sigma_um: float=60.
    envelope_support: float=.45
    envelope_growth_um: float=650.
    envelope_close_um: float=70.
    envelope_open_um: float=250.
    envelope_max_area_ratio: float=4.


def _edge_notches(body,occupancy,blocked,cfg,pitch,pixel_area):
    """A virtual wall closes ONLY bays whose mouth is on one frame edge.

    Apply each wall independently: no corner-to-corner filling. Require a
    bounded depth, nontrivial area, texture support, and no other instance.
    """
    result=body.copy()
    for edge in range(4):
        rotated=np.rot90(body,edge)
        if np.count_nonzero(rotated[-1])<2:continue
        padded=np.pad(rotated.astype(np.uint8),1);padded[-1,:]=1
        added=np.rot90(_body(padded)[1:-1,1:-1]&~rotated,-edge)
        n,labels,stats,_=cv2.connectedComponentsWithStats(added.astype(np.uint8),8)
        for k in range(1,n):
            part=labels==k;ry=np.where(np.rot90(part,edge))[0]
            if (stats[k,cv2.CC_STAT_AREA]*pixel_area>=cfg.notch_min_area_mm2
                    and (body.shape[edge%2]-ry.min())*pitch<=cfg.notch_max_depth_um
                    and float(occupancy[part].mean())>=cfg.notch_support
                    and not np.any(part&blocked)):
                result |= part
    return result


def revise(base,base_maps,v3,v3_maps,*,config=None):
    cfg=config or RevisionConfig()
    if any(isinstance(v,bool) or not math.isfinite(v) or v<=0 for v in asdict(cfg).values()):
        raise ValueError('Revision parameters must be positive finite numbers')
    for key in ('normal_fine_ratio','notch_support','merge_bridge_support','envelope_support'):
        if getattr(cfg,key)>=1:raise ValueError(key+' must be below one')
    if cfg.bright_mean_gray>255 or cfg.envelope_max_area_ratio<=1:raise ValueError('Invalid envelope parameter')
    result=deepcopy(v3)
    result.update(algorithm='experimental_feedback_rules_v4',v3_config=v3['config'],config=asdict(cfg))
    gray=base_maps['gray'];occ=base_maps['occupancy'];shape=gray.shape
    input_shape=(base['input_size']['height'],base['input_size']['width'])
    pixel_area=base['working_grid']['mm_per_pixel_x']*base['working_grid']['mm_per_pixel_y']
    pitch=math.sqrt(pixel_area)*1000
    labels=v3_maps['proposal_labels'];blocked=v3_maps['exclusion_mask']>0
    low=base_maps['low_visibility']>0
    bodies={int(c['id'][1:]):labels==int(c['id'][1:]) for c in result['components']}
    items={int(c['id'][1:]):c for c in result['components']}
    reasons={k:[] for k in bodies};sources={k:[k] for k in bodies}
    for k,c in items.items():
        before=bodies[k]
        if (c['status']=='normal_candidate' and not c['excluded_dark_source_ids']
                and float(gray[before].mean())>=cfg.bright_mean_gray
                and c['fine_texture_ratio']>=cfg.normal_fine_ratio):
            opened=_largest(cv2.morphologyEx(before.astype(np.uint8),cv2.MORPH_OPEN,_kernel(cfg.normal_open_um,pitch))>0)
            if opened.any() and not np.array_equal(opened,before):
                bodies[k]=opened;reasons[k].append('unsupported_narrow_lobe_removed')
        elif c['status']=='texture_review' and c['touch_image_border']:
            completed=_edge_notches(before,occ,blocked|((labels>0)&~before),cfg,pitch,pixel_area)
            if not np.array_equal(completed,before):
                bodies[k]=completed;reasons[k].append('bounded_frame_edge_notch_completed')

    # Build a graph from frozen evidence; enumeration order cannot affect merges.
    eligible=[k for k,c in items.items() if c['status']=='texture_review'
              and not c['touch_image_border'] and not c['excluded_dark_source_ids']
              and c['area_mm2']<=cfg.fragment_max_area_mm2
              and float(gray[bodies[k]].mean())>=cfg.bright_mean_gray]
    parent={k:k for k in eligible}
    def root(k):
        while parent[k]!=k:k=parent[k]
        return k
    smooth=cv2.GaussianBlur(occ,(0,0),cfg.envelope_sigma_um/pitch)
    distances={k:cv2.distanceTransform((~bodies[k]).astype(np.uint8),cv2.DIST_L2,5)*pitch for k in eligible}
    for i,k in enumerate(sorted(eligible)):
        for j in sorted(eligible)[i+1:]:
            gap=float(distances[k][bodies[j]].min())
            if gap>cfg.merge_gap_um:continue
            bridge=(distances[k]+distances[j]<=gap+2*pitch)&~(bodies[k]|bodies[j])
            if (bridge.any() and not np.any(bridge&(blocked|((labels>0)&~(bodies[k]|bodies[j]))))
                    and float(smooth[bridge].mean())>=cfg.merge_bridge_support):
                a,b=root(k),root(j);parent[max(a,b)]=min(a,b)
    groups={}
    for k in sorted(eligible):groups.setdefault(root(k),[]).append(k)
    for members in groups.values():
        if len(members)<2:continue
        k=min(members);seed=np.isin(labels,members)
        distance=cv2.distanceTransform((~seed).astype(np.uint8),cv2.DIST_L2,5)*pitch
        foreign=(labels>0)&~seed
        permitted=~foreign&~blocked&~low
        allowed=(((smooth>=cfg.envelope_support)&(distance<=cfg.envelope_growth_um))|seed)&permitted
        closed=cv2.morphologyEx(allowed.astype(np.uint8),cv2.MORPH_CLOSE,_kernel(cfg.envelope_close_um,pitch))>0
        n,cl=cv2.connectedComponents((closed&permitted).astype(np.uint8))
        seed_ids=np.unique(cl[seed]);seed_ids=seed_ids[seed_ids>0]
        # Both fragments must be connected by observed weak texture.
        if len(seed_ids)!=1:continue
        envelope=_body(cl==seed_ids[0])
        opened=_largest(cv2.morphologyEx(envelope.astype(np.uint8),cv2.MORPH_OPEN,_kernel(cfg.envelope_open_um,pitch))>0)
        body=(opened|seed)&permitted
        if (np.any(body&foreign) or cv2.connectedComponents(body.astype(np.uint8))[0]!=2
                or body.sum()>seed.sum()*cfg.envelope_max_area_ratio):continue
        bodies[k]=body;sources[k]=members;reasons[k].append('connected_sparse_review_fragments_merged')
        for j in members:
            if j!=k:del bodies[j]
        items[k]['source_ids']=sorted({s for j in members for s in items[j]['source_ids']})
        items[k]['v2_source_ids']=sorted({s for j in members for s in items[j]['v2_source_ids']})
        items[k]['status']='texture_review'
        # Appearance statistics from the earlier stage describe source fragments,
        # not the new envelope. Keep them explicitly as provenance.
        items[k]['source_fragment_metrics']=[{key:items[j].get(key) for key in
            ('id','area_mm2','fine_texture_ratio','seed_radius_um')} for j in members]
        items[k]['fine_texture_ratio']=sum(items[j]['fine_texture_ratio']*items[j]['area_mm2'] for j in members)/sum(items[j]['area_mm2'] for j in members)
        items[k]['seed_radius_um']=max(items[j]['seed_radius_um'] for j in members)

    changes=[];new_labels=np.zeros(shape,np.int32);accepted=np.zeros(shape,np.uint8);safe_labels=np.zeros(shape,np.int32)
    for k in sorted(bodies):
        c=items[k];body=bodies[k];before=np.isin(labels,sources[k])
        c['v3_source_ids']=[f'C{j:02d}' for j in sources[k]];c['v4_changes']=reasons[k]
        c['is_pickable']=False;c['is_valid_for_compensation']=False
        usable=body&(occ>=v3['v2_config']['boundary_occupancy'])&~low&~blocked&(base_maps['dark_mask']==0)
        if reasons[k]:
            c.update(_geometry(body,input_shape,pixel_area));c['size_is_lower_bound']=c['touch_image_border']
            d=_distance(usable);yy,xx=np.unravel_index(int(d.argmax()),shape)
            c.update(safe_area_mm2=float(usable.sum())*pixel_area,safe_radius_um=float(d.max())*pitch,
                     center_pixel=[round((xx+.5)*input_shape[1]/shape[1]-.5),round((yy+.5)*input_shape[0]/shape[0]-.5)] if usable.any() else None,
                     center_is_valid=bool(usable.any()))
            changes.append(dict(id=c['id'],v3_source_ids=c['v3_source_ids'],reasons=reasons[k],
                area_before_mm2=float(before.sum())*pixel_area,area_after_mm2=c['area_mm2'],
                removed_mm2=float((before&~body).sum())*pixel_area,added_mm2=float((body&~before).sum())*pixel_area))
        if np.any(body&(new_labels>0)):raise ValueError('V4 bodies overlap')
        new_labels[body]=k
        if c['status']=='normal_candidate':accepted[usable]=255;safe_labels[usable]=k
    result['components']=[items[k] for k in sorted(bodies)]
    for c in result['dark_regions']:
        c['v3_source_ids']=[c['id']];c['v4_changes']=[]
        c['is_pickable']=False;c['is_valid_for_compensation']=False
    result['revision_changes']=changes
    result['limitations']=[*v3['limitations'],'V4 is tuned on the same seven frames; sparse-envelope merges require human review and independent validation.']
    return result,dict(gray=gray,occupancy=occ,proposal_labels=new_labels,accepted_mask=accepted,
        safe_labels=safe_labels,exclusion_mask=v3_maps['exclusion_mask'].copy(),boundary_support=v3_maps['boundary_support'].copy())


def analyze(gray,*,fov_mm,config=None):
    base,bm=segment_single_image(gray,fov_mm=fov_mm)
    v2,m2=refine_evidence(base,bm);v3,m3=revise_v3(base,bm,v2,m2)
    return revise(base,bm,v3,m3,config=config)
