"""Image-only V3 experiment: contact necks, shadow tails, and sparse rims.

Human polygons are evaluation inputs in the report tool, never inference inputs.
V2 instance IDs are preserved; this revision does not introduce new split/merge.
"""
from copy import deepcopy
from dataclasses import asdict,dataclass
import math
import cv2
import numpy as np

from .single_image_segment import segment_single_image
from .single_image_refine_v2 import refine_evidence,_body,_distance,_geometry,_kernel,_raster


@dataclass(frozen=True)
class RevisionConfig:
    contact_size_ratio: float=2.5
    contact_band_um: float=240.
    contact_open_radius_um: float=180.
    shadow_texture_occupancy: float=.50
    shadow_texture_reach_um: float=15.
    shadow_graft_margin_um: float=60.
    sparse_mean_gray: float=205.
    sparse_fine_ratio: float=.37
    sparse_support_occupancy: float=.50
    sparse_growth_um: float=420.
    sparse_close_radius_um: float=50.
    sparse_open_radius_um: float=65.
    promotion_seed_radius_um: float=180.
    promotion_safe_area_mm2: float=.30
    promotion_solidity: float=.75
    promotion_fine_ratio: float=.32


def _largest(mask):
    n,labels,stats,_=cv2.connectedComponentsWithStats(mask.astype(np.uint8),8)
    return labels==(1+int(np.argmax(stats[1:,cv2.CC_STAT_AREA]))) if n>1 else np.zeros_like(mask,bool)


def revise(base,base_maps,v2,v2_maps,*,config=None):
    cfg=config or RevisionConfig()
    if any(isinstance(v,bool) or not math.isfinite(v) or v<=0 for v in asdict(cfg).values()):
        raise ValueError('Revision parameters must be positive finite numbers')
    for key in ('shadow_texture_occupancy','sparse_support_occupancy','promotion_solidity'):
        if getattr(cfg,key)>=1:raise ValueError(f'{key} must be below one')
    result=deepcopy(v2)
    result.update(algorithm='experimental_feedback_rules_v3',v2_config=v2['config'],config=asdict(cfg))
    gray=base_maps['gray'].astype(np.float32);occupancy=base_maps['occupancy'];shape=gray.shape
    input_shape=(base['input_size']['height'],base['input_size']['width'])
    mx=base['working_grid']['mm_per_pixel_x'];my=base['working_grid']['mm_per_pixel_y']
    pixel_area=mx*my;pitch_um=math.sqrt(pixel_area)*1000
    low=base_maps['low_visibility']>0
    old_labels=base_maps['proposal_labels'];v2_labels=v2_maps['proposal_labels']
    bodies={int(c['id'][1:]):v2_labels==int(c['id'][1:]) for c in result['components']}
    reference_bodies={number:body.copy() for number,body in bodies.items()}
    nearest=np.zeros(shape,np.int32);nearest_distance=np.full(shape,np.inf,np.float32)
    for number in sorted(reference_bodies):
        distance=cv2.distanceTransform((~reference_bodies[number]).astype(np.uint8),cv2.DIST_L2,5)
        closer=distance<nearest_distance;nearest[closer]=number;nearest_distance[closer]=distance[closer]
    all_v2=v2_labels>0
    darks={c['id']:(c,_raster(c,shape,input_shape)&(base_maps['dark_mask']>0)) for c in base['dark_regions']}
    changes=[]

    for item in result['components']:
        number=int(item['id'][1:]);before=reference_bodies[number];body=before.copy();reasons=[]
        # Remove unsupported parts of edge-darkness grafts. Dark evidence can
        # explain shading but cannot establish the outside contour by itself.
        source_labels=[int(s[1:]) for s in item['source_ids'] if s.startswith('C')]
        seed_body=_body(np.isin(old_labels,source_labels))
        graft=np.zeros(shape,bool)
        for source_id in item['source_ids']:
            if source_id in darks and darks[source_id][0]['touch_image_border']:
                graft |= darks[source_id][1] & ~seed_body
        if graft.any():
            graft_zone=cv2.dilate(graft.astype(np.uint8),_kernel(cfg.shadow_graft_margin_um,pitch_um))>0
            textured=(occupancy>=cfg.shadow_texture_occupancy)&body&~low
            near_texture=cv2.distanceTransform((~textured).astype(np.uint8),cv2.DIST_L2,5)*pitch_um
            remove=graft_zone&body&~seed_body&(near_texture>cfg.shadow_texture_reach_um)
            if remove.any():
                body=_largest(body&~remove);reasons.append('unsupported_shadow_tail_removed')

        # Retract narrow appendages of a much larger body at a nearby separate
        # body. Do not globally erode either colony or fuse the two contours.
        near_small=np.zeros(shape,bool)
        for other in result['components']:
            j=int(other['id'][1:])
            if j==number or item['area_mm2']<cfg.contact_size_ratio*other['area_mm2']:continue
            dist=cv2.distanceTransform((~reference_bodies[j]).astype(np.uint8),cv2.DIST_L2,5)*pitch_um
            near_small |= dist<=cfg.contact_band_um
        if (body&near_small).any():
            opened=cv2.morphologyEx(body.astype(np.uint8),cv2.MORPH_OPEN,
                                   _kernel(cfg.contact_open_radius_um,pitch_um))>0
            remove=body&~opened&near_small
            if remove.any():
                body=_largest(body&~remove);reasons.append('narrow_contact_appendage_retracted')

        # Washed-out fine texture may have a dense interior and a dilute rim.
        # Permit connected weak evidence to grow farther only for this appearance;
        # the usable mask below continues to demand the stronger texture support.
        if (item['status']=='normal_candidate' and not item['excluded_dark_source_ids']
                and float(gray[before].mean())>=cfg.sparse_mean_gray
                and item['fine_texture_ratio']>=cfg.sparse_fine_ratio):
            distance=cv2.distanceTransform((~body).astype(np.uint8),cv2.DIST_L2,5)*pitch_um
            allowed=((occupancy>=cfg.sparse_support_occupancy)&~low
                     &(distance<=cfg.sparse_growth_um)&(~all_v2|before)&(nearest==number))|body
            closed=cv2.morphologyEx(allowed.astype(np.uint8),cv2.MORPH_CLOSE,
                                   _kernel(cfg.sparse_close_radius_um,pitch_um))>0
            closed &= (~all_v2|before)&~low&(nearest==number)
            n,labels=cv2.connectedComponents(closed.astype(np.uint8))
            ids=np.unique(labels[body]);ids=ids[ids>0]
            grown=_largest(_body(np.isin(labels,ids)))
            core=cv2.morphologyEx(grown.astype(np.uint8),cv2.MORPH_OPEN,
                                 _kernel(cfg.sparse_open_radius_um,pitch_um))>0
            grown=_largest(core)|body
            if (grown&~body).any():
                body=grown;reasons.append('connected_sparse_fine_texture_rim_recovered')

        changed=not np.array_equal(body,before)
        if changed:
            item.update(_geometry(body,input_shape,pixel_area))
            item['size_is_lower_bound']=item['touch_image_border']
        usable=body&(occupancy>=v2['config']['boundary_occupancy'])&~low
        usable &= (base_maps['dark_mask']==0)&(v2_maps['exclusion_mask']==0)
        safe_distance=_distance(usable)
        safe_area=float(usable.sum())*pixel_area
        safe_radius=float(safe_distance.max())*pitch_um
        if (item['status']=='texture_review' and not item['excluded_dark_source_ids']
                and item['seed_radius_um']>=cfg.promotion_seed_radius_um
                and safe_area>=cfg.promotion_safe_area_mm2 and item['solidity']>=cfg.promotion_solidity
                and item['fine_texture_ratio']>=cfg.promotion_fine_ratio):
            item['status']='normal_candidate';reasons.append('sustained_texture_core_reclassified_normal')
        if changed:
            yy,xx=np.unravel_index(int(np.argmax(safe_distance)),shape)
            item.update(safe_area_mm2=safe_area,safe_radius_um=safe_radius,
                        center_pixel=[round((xx+.5)*input_shape[1]/shape[1]-.5),round((yy+.5)*input_shape[0]/shape[0]-.5)] if usable.any() else None,
                        center_is_valid=bool(usable.any()))
        item['v2_source_ids']=[item['id']]
        item['v3_changes']=reasons
        if reasons:
            changes.append(dict(id=item['id'],reasons=reasons,area_before_mm2=float(before.sum())*pixel_area,
                                area_after_mm2=float(body.sum())*pixel_area,removed_mm2=float((before&~body).sum())*pixel_area,
                                added_mm2=float((body&~before).sum())*pixel_area))
        bodies[number]=body

    labels=np.zeros(shape,np.int32);accepted=np.zeros(shape,np.uint8);safe_labels=np.zeros(shape,np.int32)
    for c in result['components']:
        number=int(c['id'][1:]);body=bodies[number]
        if np.any(body&(labels>0)):raise ValueError('V3 body overlaps another instance')
        labels[body]=number
        if c['status']=='normal_candidate':
            safe=body&(occupancy>=v2['config']['boundary_occupancy'])&~low
            safe &= (base_maps['dark_mask']==0)&(v2_maps['exclusion_mask']==0)
            accepted[safe]=255;safe_labels[safe]=number
    for c in result['dark_regions']:c['v2_source_ids']=[c['id']];c['v3_changes']=[]
    result['revision_changes']=changes
    result['limitations']=[*v2['limitations'],'V3 thresholds adjusted on the same reviewed frames; polygons are evaluation-only, not an independent test.']
    return result,dict(gray=base_maps['gray'],occupancy=occupancy,proposal_labels=labels,accepted_mask=accepted,
                       safe_labels=safe_labels,exclusion_mask=v2_maps['exclusion_mask'],
                       boundary_support=v2_maps['boundary_support'])


def analyze(gray,*,fov_mm,config=None):
    base,base_maps=segment_single_image(gray,fov_mm=fov_mm)
    v2,v2_maps=refine_evidence(base,base_maps)
    return revise(base,base_maps,v2,v2_maps,config=config)
