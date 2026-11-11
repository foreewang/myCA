"""Public task/vision/storage alarm catalog; never interpolate exception text."""

TASK_ALARM_CODES = {
    "TASK_FAILED": ("task", "P1", "任务执行失败，请检查关联设备报警或本地日志"),
    "TASK_INTERRUPTED": ("task", "P1", "任务因服务中断而未正常结束"),
    "TASK_CANCELED": ("task", "INFO", "任务已取消"),
    "TASK_WORKER_UNAVAILABLE": ("runtime", "P1", "任务执行线程不可用"),
    "TASK_RECORD_READ_FAILED": ("storage", "P1", "任务记录读取失败或格式异常"),
    "RESULT_READ_FAILED": ("storage", "P1", "任务结果文件读取失败或格式异常"),
    "STORAGE_WRITE_FAILED": ("storage", "P1", "本地数据写入失败"),
    "CONFIG_EXECUTION_BLOCKED": ("configuration", "P1", "运行配置异常，任务无法执行"),
    "VISION_MODEL_FAILED": ("vision", "P1", "视觉模型加载或运行失败"),
    "VISION_INFERENCE_FAILED": ("vision", "P1", "视觉推理失败或返回结果异常"),
    "VISION_INPUT_QUALITY_FAILED": ("vision", "P1", "视觉输入图像质量不符合检测要求"),
    "VISION_GPU_DEGRADED": ("vision", "P2", "视觉计算已从 GPU 降级至 CPU"),
    "COMPENSATION_OUT_OF_TOLERANCE": ("vision", "P1", "闭环补偿结束时目标仍未达到允许偏差"),
    "API_UNHANDLED_ERROR": ("runtime", "P1", "接口处理发生内部异常，请通过诊断标识查询本地日志"),
}
