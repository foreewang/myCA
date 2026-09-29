"""Backend contracts, exact morphology, and unchanged default request routing."""
import importlib
from concurrent.futures import ThreadPoolExecutor
import cv2
import numpy as np
import pytest
from vision.vision import single_image_gpu as gpu
from vision.vision.detect_pipeline import detect_from_gray
from tests.test_accepted_single_image_rules import sample


@pytest.fixture(autouse=True)
def clear_failure():
    gpu.reset_backend()
    yield
    gpu.reset_backend()


@pytest.mark.parametrize('error', [ImportError('cupy missing'), RuntimeError('NVRTC failed'), MemoryError('GPU out of memory')])
def test_auto_fallback_is_exact_remembered_logged_and_strict_cuda_retries(monkeypatch, caplog, error):
    calls = []
    def fail(*args):
        calls.append(1)
        raise error
    monkeypatch.setattr(gpu, '_cuda_close', fail)
    image = np.random.default_rng(1).uniform(0,255,(31,41)).astype(np.float32)
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(7,7))
    expected = cv2.morphologyEx(image,cv2.MORPH_CLOSE,kernel)
    for _ in range(2):
        actual, info = gpu.background_close(image,kernel)
        np.testing.assert_array_equal(actual,expected)
        assert info['backend']=='cpu' and str(error) in info['fallback_reason']
    assert len(calls)==1 and 'auto uses CPU' in caplog.text
    with pytest.raises(RuntimeError,match='CUDA background closing failed'):
        gpu.background_close(image,kernel,'cuda')
    assert len(calls)==2
    gpu.background_close(image,kernel,'cpu')
    assert len(calls)==2
    gpu.reset_backend()
    gpu.background_close(image,kernel)
    assert len(calls)==3


@pytest.mark.parametrize('alias',['vision.vision.detect_pipeline:process_image','vision.detect_pipeline:process_image'])
def test_unchanged_request_uses_auto_and_reports_fallback(tmp_path,monkeypatch,alias):
    from workflow.detect_api import run_detect_on_image, rule_texture_kwargs, _resolve_callable
    from vision.vision.image_loader import save_image
    resolved = _resolve_callable(alias)
    module=importlib.import_module(resolved.__module__.rsplit('.',1)[0]+'.single_image_gpu')
    def unavailable(*args):
        raise ImportError('test GPU unavailable')
    module.reset_backend()
    monkeypatch.setattr(module,'_cuda_close',unavailable)
    path=tmp_path/'frame.bmp';save_image(path,sample())
    config={'entrypoint':alias,'save_overlay':True,'overlay_source':'vision',
            'detect_well_border':True,'well_border_margin_mm':0.,'well_border_margin_px':30.}
    options=rule_texture_kwargs(alias,config)
    assert 'texture_backend' not in options
    try:
        raw=run_detect_on_image(path,entrypoint=alias,detect_kwargs={**options,'mm_per_pixel':2/640})['raw_result']
        meta=raw['texture_processing']
        assert meta['requested_backend']=='auto'
        assert meta['stage_backends']=={'background_close':'cpu','remaining_stages':'cpu'}
        assert 'test GPU unavailable' in meta['fallback_reason']
        cpu=detect_from_gray(sample(),fov_mm=(2.,2.),texture_backend='cpu')
        assert raw['components']==cpu['components']
    finally:
        module.reset_backend()


def cuda_available():
    cp=pytest.importorskip('cupy')
    try:
        if cp.cuda.runtime.getDeviceCount()<1:
            pytest.skip('CUDA device unavailable')
    except cp.cuda.runtime.CUDARuntimeError:
        pytest.skip('CUDA runtime unavailable')


@pytest.mark.parametrize('shape,size', [((13,17),3),((13,17),31),((1,1),255),((64,71),7),((1024,1024),255)])
def test_cuda_exact_for_variable_kernels_and_image_edges(shape,size):
    cuda_available()
    image=np.random.default_rng(3).uniform(-20,255,shape).astype(np.float32)
    kernel=cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(size,size))
    actual,info=gpu.background_close(image,kernel,'cuda')
    expected=cv2.morphologyEx(image,cv2.MORPH_CLOSE,kernel)
    np.testing.assert_array_equal(actual,expected)
    assert info=={'backend':'cuda','fallback_reason':None}


def test_cuda_full_results_outputs_and_concurrent_calls(tmp_path):
    cuda_available()
    image=sample()
    cpu=detect_from_gray(image,fov_mm=(2.,2.),texture_backend='cpu',out_dir=tmp_path/'cpu')
    accelerated=detect_from_gray(image,fov_mm=(2.,2.),out_dir=tmp_path/'gpu')
    assert accelerated['texture_processing']['stage_backends']['background_close']=='cuda'
    for key in ('components','review_candidates','rule_stage_configs','suppressed_candidates'):
        assert cpu[key]==accelerated[key]
    for name in ('05_contour_mask.bmp','06_overlay.bmp'):
        a=cv2.imdecode(np.fromfile(tmp_path/'cpu'/name,np.uint8),-1)
        b=cv2.imdecode(np.fromfile(tmp_path/'gpu'/name,np.uint8),-1)
        np.testing.assert_array_equal(a,b)
    kernel=cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(31,31))
    inputs=[np.random.default_rng(i).uniform(0,255,(64,71)).astype(np.float32) for i in range(4)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        outputs=list(pool.map(lambda a:gpu.background_close(a,kernel,'cuda')[0],inputs))
    for a,b in zip(inputs,outputs):
        np.testing.assert_array_equal(b,cv2.morphologyEx(a,cv2.MORPH_CLOSE,kernel))
