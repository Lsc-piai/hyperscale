# app/audio/decoder.py
import ffmpeg
import numpy as np
import os
import tempfile
import subprocess

def _decode_via_pipe(data: bytes, target_sr: int) -> np.ndarray:
    proc = (
        ffmpeg
        .input("pipe:0")
        .output("pipe:1", format="f32le", acodec="pcm_f32le", ac=1, ar=target_sr, vn=None)
        .run_async(pipe_stdin=True, pipe_stdout=True, pipe_stderr=True, quiet=True)
    )
    out, err = proc.communicate(input=data)
    audio = np.frombuffer(out, dtype=np.float32).copy()
    if audio.size == 0:
        raise RuntimeError(f"pipe decode failed: {err.decode(errors='ignore')}")
    return audio

def _decode_via_memfd(data: bytes, target_sr: int) -> np.ndarray:
    if not hasattr(os, "memfd_create"):
        raise OSError("memfd_create not supported on this platform")
    fd = os.memfd_create("upload_audio", flags=0)
    try:
        os.write(fd, data)
        path = f"/proc/self/fd/{fd}"
        cmd = [
            "ffmpeg","-nostdin","-hide_banner","-loglevel","error",
            "-analyzeduration","200M","-probesize","200M",
            "-i", path,
            "-vn","-f","f32le","-acodec","pcm_f32le",
            "-ac","1","-ar",str(target_sr),
            "pipe:1",
        ]
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        audio = np.frombuffer(proc.stdout, dtype=np.float32).copy()
        if audio.size == 0:
            raise RuntimeError(proc.stderr.decode(errors='ignore'))
        return audio
    finally:
        try: os.close(fd)
        except: pass

def _decode_via_tmpfs(data: bytes, target_sr: int, ext_hint: str) -> np.ndarray:
    # /dev/shm 는 tmpfs(메모리) — 디스크 쓰지 않음
    suffix = ext_hint if ext_hint.startswith(".") else f".{ext_hint}" if ext_hint else ".bin"
    with tempfile.NamedTemporaryFile(dir="/dev/shm", suffix=suffix, delete=False) as tmp:
        tmp.write(data)
        tmp_path = tmp.name
    try:
        cmd = [
            "ffmpeg","-nostdin","-hide_banner","-loglevel","error",
            "-analyzeduration","200M","-probesize","200M",
            "-i", tmp_path,
            "-vn","-f","f32le","-acodec","pcm_f32le",
            "-ac","1","-ar",str(target_sr),
            "pipe:1",
        ]
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        audio = np.frombuffer(proc.stdout, dtype=np.float32).copy()
        if audio.size == 0:
            raise RuntimeError(proc.stderr.decode(errors='ignore'))
        return audio
    finally:
        try: os.unlink(tmp_path)
        except: pass

def decode_audio_bytes_to_numpy(data: bytes, ext: str | None = None, target_sr: int = 16000):
    """
    업로드 바이트 → float32 모노 16kHz 1D NumPy (T,) 반환.
    1) pipe 시도 → 2) memfd 시도 → 3) /dev/shm tmpfs 파일 경유.
    """
    ext = (ext or "").lower()
    # 1) pipe (빠름, mkv/webm/wav/mp3 등에서 종종 성공)
    try:
        audio = _decode_via_pipe(data, target_sr)
        return audio, target_sr
    except Exception as e_pipe:
        pipe_err = str(e_pipe)

    # 2) memfd (플랫폼 지원 시 디스크 없이 seek 가능)
    try:
        audio = _decode_via_memfd(data, target_sr)
        return audio, target_sr
    except Exception as e_memfd:
        memfd_err = str(e_memfd)

    # 3) /dev/shm (RAM 디스크) — mp4/m4a 같은 seek 요구 컨테이너도 안전
    try:
        audio = _decode_via_tmpfs(data, target_sr, ext_hint=ext)
        return audio, target_sr
    except Exception as e_tmp:
        tmp_err = str(e_tmp)
        raise RuntimeError(
            f"Decoded audio failed. pipe_err={pipe_err} ; memfd_err={memfd_err} ; tmpfs_err={tmp_err}"
        )