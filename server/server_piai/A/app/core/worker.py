# app/core/worker.py
from __future__ import annotations
import threading, queue, time, uuid
from dataclasses import dataclass, field
from typing import Callable, Optional

@dataclass
class Job:
    id: str
    filename: str
    data: bytes = field(repr=False)

# 전역
JOB_QUEUE: "queue.Queue[Job]" = queue.Queue(maxsize=100)  # 백프레셔 원하면 maxsize 유지
_STOP_EVENT = threading.Event()
_PROCESS_FN: Optional[Callable[[Job], None]] = None

def set_processor(fn: Callable[[Job], None]) -> None:
    global _PROCESS_FN
    _PROCESS_FN = fn

def enqueue(filename: str, data: bytes) -> str:
    job_id = uuid.uuid4().hex[:12]
    job = Job(id=job_id, filename=filename, data=data)
    JOB_QUEUE.put(job)  # 가득 차면 여기서 block (또는 put_nowait로 Full 처리)
    return job_id  # 굳이 안 써도 되지만 서버 로그용으로는 유용

def start_worker(num_threads: int = 1) -> None:
    for i in range(num_threads):
        t = threading.Thread(target=_worker_loop, name=f"worker-{i}", daemon=True)
        t.start()

def stop_worker() -> None:
    _STOP_EVENT.set()

def _worker_loop() -> None:
    while not _STOP_EVENT.is_set():
        try:
            job = JOB_QUEUE.get(timeout=0.5)
        except queue.Empty:
            continue
        t0 = time.perf_counter()
        try:
            if _PROCESS_FN is None:
                raise RuntimeError("PROCESS_FN not set")
            _PROCESS_FN(job)   # ← 결과/상태 저장 없음. 실패하면 except 로만 기록.
        except Exception as e:
            # process_fn 안에서 터지면 그쪽 [TIMING] 결산이 안 찍히는 경로도 있으므로
            # (디코딩·STT 단계의 예외 등) 여기서 최소한 총 소요시간은 남긴다.
            print(f"[worker] job {job.id} failed after {time.perf_counter() - t0:.2f}s: {e}",
                  flush=True)
            pass
        finally:
            # 메모리 점유 줄이기: 큰 data를 바로 해제
            job.data = b""
            JOB_QUEUE.task_done()