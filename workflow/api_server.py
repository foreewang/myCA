from __future__ import annotations

import os
from typing import Any, Dict

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from workflow.run_task import execute_task_request

app = FastAPI(title="Colony Workflow API", version="0.1.0")


class ExecuteTaskRequest(BaseModel):
    task: Dict[str, Any]
    camera_path: str | None = Field(default=None, description="可选，覆盖默认 camera.yaml")
    objectives_path: str | None = Field(default=None, description="可选，覆盖默认 objectives.yaml")
    plates_path: str | None = Field(default=None, description="可选，覆盖默认 plates.yaml")
    dump_json: str | None = Field(default=None, description="可选，覆盖结果落盘路径")
    persist_result: bool = Field(default=True, description="是否仍然把结果写到本地文件")


@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}


@app.post("/api/tasks/execute")
def execute_task(req: ExecuteTaskRequest) -> Dict[str, Any]:
    try:
        result = execute_task_request(
            raw_task_cfg={"task": req.task},
            camera_path=req.camera_path or os.getenv("CAMERA_CONFIG_PATH"),
            objectives_path=req.objectives_path or os.getenv("OBJECTIVES_CONFIG_PATH"),
            plates_path=req.plates_path or os.getenv("PLATES_CONFIG_PATH"),
            dump_json=req.dump_json,
            persist_result=req.persist_result,
        )
        return result
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
