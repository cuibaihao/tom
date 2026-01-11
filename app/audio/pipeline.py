from __future__ import annotations

import time
from pathlib import Path




from app.audio.audio_loader import ffprobe_duration_ms, transcode_to_wav_16k_mono
from app.audio.asr import ASR
from app.audio.segmenter import merge_by_max_duration
from app.db import audio_db
from app.deps import get_audio_vs



import json, math, os, subprocess
from dataclasses import dataclass

from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np
import soundfile as sf

import webrtcvad
from faster_whisper import WhisperModel
from langchain_core.documents import Document





ProgressFn = Callable[[int, str], None]

TARGET_SR = int(os.getenv("AUDIO_SR", "16000"))  # 目标采样率：默认16000Hz
TARGET_CH = int(os.getenv("AUDIO_CH", "1"))  # 目标声道数：默认1（单声道）

VAD_MODE = int(os.getenv("VAD_MODE", "2"))  # VAD敏感度：0~3，越大越严格
VAD_FRAME_MS = int(os.getenv("VAD_FRAME_MS", "30"))  # VAD帧长：默认30ms（webrtcvad常用10/20/30）
VAD_PADDING_MS = int(os.getenv("VAD_PADDING_MS", "300"))  # 语音段前后扩展：默认300ms，防止切太死
VAD_MIN_SPEECH_MS = int(os.getenv("VAD_MIN_SPEECH_MS", "500"))  # 最短语音段：默认>=500ms才保留
VAD_MERGE_GAP_MS = int(os.getenv("VAD_MERGE_GAP_MS", "250"))  # 合并间隔：相邻段间隔<=250ms则合并

ASR_MODEL = os.getenv("ASR_MODEL", "base")  # Whisper模型名/路径：默认base
ASR_DEVICE = os.getenv("ASR_DEVICE", "cpu")  # 推理设备：默认cpu（也可能是cuda）
ASR_COMPUTE_TYPE = os.getenv("ASR_COMPUTE_TYPE", "int8")  # 计算精度：cpu常用int8省资源

MAX_CHUNK_MS = int(os.getenv("AUDIO_MAX_CHUNK_MS", "25000"))  # 单chunk最大时长：默认25s，超过强制切
MIN_CHUNK_MS = int(os.getenv("AUDIO_MIN_CHUNK_MS", "6000"))  # 单chunk最小时长阈值：默认6s，满足且遇标点可切
MAX_CHARS_PER_CHUNK = int(os.getenv("AUDIO_MAX_CHARS_PER_CHUNK", "900"))  # 单chunk最大字数：默认900，超过截断

PUNCT_END = set("。.!?！？；;")  # 认为“句子结束”的标点集合：用于断句切chunk

MAX_SPEECH_SEGMENTS = int(os.getenv("AUDIO_MAX_SPEECH_SEGMENTS", "2000"))  # VAD段上限：防止噪声导致段数爆炸


@dataclass  # 生成init/repr等，方便存取字段
class SpeechSeg:  # 表示“语音活动段”的起止时间
    start_ms: int
    end_ms: int


@dataclass
class AsrSeg:  # 表示“带文本”的识别片段（或合并chunk）
    start_ms: int
    end_ms: int
    text: str  # 该段识别文本


def run_audio_ingest_pipeline(
        *,
        audio_id: str,
        raw_path: Path,
        original_filename: str,
        visibility: str,
        language: str | None,
        wav_dir: Path,
        model_name: str = "base",
        device: str = "cpu",
        compute_type: str = "int8",
        max_chunk_ms: int = 25_000,
        min_chunk_ms: int = 6_000,
        on_progress: Optional[ProgressFn] = None,
) -> dict[str, Any]:
    def prog(p: int, msg: str) -> None:
        if on_progress:
            try:
                on_progress(int(p), str(msg))
            except Exception:
                pass

    if not audio_id:
        raise RuntimeError("audio_id required")
    if not raw_path.exists():
        raise RuntimeError(f"raw audio not found: {raw_path}")
    if not visibility:
        raise RuntimeError("visibility required")

    wav_dir.mkdir(parents=True, exist_ok=True)
    wav_path = wav_dir / f"{audio_id}.wav"

    prog(12, "transcoding")
    transcode_to_wav_16k_mono(raw_path, wav_path)

    prog(18, "probing duration")
    duration_ms = ffprobe_duration_ms(wav_path)

    prog(30, "transcribing")
    asr = ASR(model_name=model_name, device=device, compute_type=compute_type)
    asr_segs, detected_lang = asr.transcribe(str(wav_path), language=language)
    lang = language or detected_lang

    if not asr_segs:
        raise RuntimeError("ASR produced no segments")

    prog(45, "segmenting")
    chunks = merge_by_max_duration(asr_segs, max_ms=max_chunk_ms, min_ms=min_chunk_ms)
    if not chunks:
        raise RuntimeError("no chunks produced")

    prog(55, "building documents")
    docs: list[Document] = []
    segment_rows: list[dict[str, Any]] = []

    now_ts = int(time.time())
    seg_idx = 0
    for c in chunks:
        text = (c.text or "").strip()
        if not text:
            continue

        segment_id = f"{audio_id}:{seg_idx}"
        meta = {
            "doc_type": "audio",

            "audio_id": audio_id,
            "segment_id": segment_id,
            "start_ms": int(c.start_ms),
            "end_ms": int(c.end_ms),
            "visibility": visibility,

            "segment_idx": int(seg_idx),
            "original_filename": original_filename,
            "stored_path": str(raw_path),
            "wav_path": str(wav_path),
            "language": lang,
            "ingested_at": now_ts,
        }

        docs.append(Document(page_content=text, metadata=meta))
        segment_rows.append(
            {
                "segment_idx": int(seg_idx),
                "start_ms": int(c.start_ms),
                "end_ms": int(c.end_ms),
                "text": text,
            }
        )
        seg_idx += 1

    if not docs:
        raise RuntimeError("no transcript text produced")

    prog(72, "writing vectorstore")
    vs = get_audio_vs()
    vs.add_documents(docs)

    prog(85, "writing database segments")
    audio_db.replace_audio_segments(audio_id, segment_rows)
    prog(95, "done")

    return {
        "duration_ms": int(duration_ms),
        "language": lang,
        "segments": int(len(segment_rows)),
        "wav_path": str(wav_path),
    }

def _prog(cb: Optional[ProgressFn], p: int, m: str) -> None:
    """统一进度汇报：若传入回调则调用。"""  # 把进度与消息交给外部（如更新DB）
    if cb:
        cb(int(p), str(m))  # 调用回调：强制转int/str，避免外部类型不一致


def _run(cmd: List[str]) -> None:
    """运行外部命令（ffmpeg等），失败则抛异常并带stderr。"""
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)  # 执行命令，捕获stdout/stderr为文本
    if p.returncode != 0:  # 如果退出码非0表示命令失败
        raise RuntimeError(f"Command failed: {' '.join(cmd)}\nSTDERR:\n{p.stderr[:4000]}")  # 抛异常并截断stderr避免太长


def transcode_to_wav_16k_mono(src: Path, dst: Path) -> None:
    """把任意音频转为16kHz单声道WAV，写到dst。"""  # 统一格式给VAD/ASR
    dst.parent.mkdir(parents=True, exist_ok=True)
    _run([
        "/Users/peter/Downloads/ffmpeg", "-y",  # -y覆盖输出文件
        "-i", str(src),  # 输入文件路径
        "-ac", str(TARGET_CH),  # 设定声道数（1）
        "-ar", str(TARGET_SR),  # 设定采样率（16000）
        "-f", "wav",  # 输出格式强制为wav
        str(dst),  # 输出文件路径
    ])


def ffprobe_duration_ms(path: Path) -> int:
    """用ffprobe读取音频时长（毫秒）。"""
    p = subprocess.run(  # 执行ffprobe获取duration
        ["/Users/peter/Downloads/ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],  # 输出json，包含duration
        stdout=subprocess.PIPE,  # 捕获标准输出
        stderr=subprocess.PIPE,  # 捕获错误输出
        text=True,  # 用文本模式读取
    )
    if p.returncode != 0:  # 若ffprobe失败
        raise RuntimeError(f"ffprobe failed: {p.stderr[:2000]}")  # 抛异常并截断stderr
    data = json.loads(p.stdout or "{}")  # 解析stdout为json；若为空则用空对象
    dur = float((data.get("format") or {}).get("duration") or 0.0)  # 读取format.duration（秒），缺失则0
    return int(dur * 1000)  # 秒转毫秒并返回整数


def _read_wav_mono_16k(path: Path) -> np.ndarray:
    """读取16kHz wav并确保单声道，返回float32样本数组。"""  #给VAD/ASR提供统一numpy数组
    x, sr = sf.read(str(path), dtype="float32", always_2d=False)  # 读取音频：x为样本，sr为采样率；输出float32
    if sr != TARGET_SR:  # 若采样率不是目标16k
        raise RuntimeError(f"wav sample rate not {TARGET_SR}: {sr}")
    if isinstance(x, np.ndarray) and x.ndim == 2:  # 若是二维数组（多声道）
        x = x.mean(axis=1)  # 对声道取平均，混成单声道
    return np.asarray(x, dtype=np.float32)  # 强制转为float32的一维数组返回


def _float_to_pcm16_bytes(x: np.ndarray) -> bytes:
    """把[-1,1]浮点样本转为PCM16字节流（webrtcvad需要）。"""  # webrtcvad输入必须是16-bit PCM bytes
    x = np.clip(x, -1.0, 1.0)  # 限幅，防止溢出到int16范围外
    pcm = (x * 32767.0).astype(np.int16)  # 映射到int16范围并转换类型
    return pcm.tobytes()  # 转为原始字节流返回


def detect_speech_segments(wav_path: Path) -> List[SpeechSeg]:
    """对wav做VAD，输出语音段列表（毫秒）。"""  # 找出“有人说话”的时间区间，减少ASR计算量
    x = _read_wav_mono_16k(wav_path)  # 读取16k单声道样本
    pcm_bytes = _float_to_pcm16_bytes(x)  # 转成PCM16字节给VAD使用

    vad = webrtcvad.Vad(VAD_MODE)  # 创建VAD对象，mode越大越严格

    frame_len = int(TARGET_SR * (VAD_FRAME_MS / 1000.0))  # 每帧样本数：采样率 * 帧时长(秒)
    frame_bytes = frame_len * 2  # 每帧字节数：int16每样本2字节
    total_frames = len(pcm_bytes) // frame_bytes  # 总帧数：按帧长整除（尾部不足一帧丢弃）

    def is_speech(i: int) -> bool:
        """判断第i帧是否语音。"""  # 封装vad.is_speech并处理边界
        start = i * frame_bytes  # 当前帧起始字节偏移
        chunk = pcm_bytes[start:start + frame_bytes]  # 取出当前帧的PCM字节
        if len(chunk) < frame_bytes:  # 如果不足一帧
            return False  # 直接认为非语音
        return vad.is_speech(chunk, sample_rate=TARGET_SR)  # 调webrtcvad判断该帧是否语音

    speech_frames: List[Tuple[int, int]] = []  # 存储语音帧段（起帧索引, 结束帧索引）
    in_speech = False  # 当前是否处于“语音段”内
    seg_start = 0  # 当前语音段起始帧索引

    for i in range(total_frames):  # 遍历所有帧
        sp = is_speech(i)  # 判断第i帧是否语音
        if sp and not in_speech:  # 如果检测到语音且之前不在语音段
            in_speech = True  # 进入语音段状态
            seg_start = i  # 记录语音段起始帧
        elif (not sp) and in_speech:  # 如果检测到非语音且之前在语音段
            in_speech = False  # 退出语音段状态
            speech_frames.append((seg_start, i))  # 记录一段语音（起始到当前帧i为止，i是终止边界）

    if in_speech:  # 如果结束时仍在语音段（音频结尾在说话）
        speech_frames.append((seg_start, total_frames))  # 把最后一段补上，终止为总帧数

    pad_frames = int(math.ceil(VAD_PADDING_MS / VAD_FRAME_MS))  # padding换算成帧数（向上取整）
    out: List[SpeechSeg] = []  # 输出语音段（毫秒）
    for a, b in speech_frames:  # 遍历每个原始语音帧段
        a2 = max(0, a - pad_frames)  # 起始往前扩padding帧，但不小于0
        b2 = min(total_frames, b + pad_frames)  # 结束往后扩padding帧，但不超过total_frames
        start_ms = int(a2 * VAD_FRAME_MS)  # 起始帧索引→毫秒（帧长ms）
        end_ms = int(b2 * VAD_FRAME_MS)  # 结束帧索引→毫秒
        if (end_ms - start_ms) >= VAD_MIN_SPEECH_MS:  # 如果该段长度达到最小语音阈值
            out.append(SpeechSeg(start_ms=start_ms, end_ms=end_ms))  # 加入输出列表

    if not out:  # 如果完全没检测到合格语音段
        return []  # 返回空列表

    merged: List[SpeechSeg] = [out[0]]  # 合并后的列表，先放第一段
    for s in out[1:]:  # 逐段尝试与前一段合并
        prev = merged[-1]  # 取合并列表的最后一段
        if s.start_ms - prev.end_ms <= VAD_MERGE_GAP_MS:  # 如果两段间隔小于等于合并阈值
            prev.end_ms = max(prev.end_ms, s.end_ms)  # 合并：扩展前一段结束时间
        else:  # 否则间隔太大不合并
            merged.append(s)  # 直接作为新段加入

    if len(merged) > MAX_SPEECH_SEGMENTS:  # 如果语音段数量超过上限
        merged = merged[:MAX_SPEECH_SEGMENTS]  # 截断，防止极端情况拖垮系统

    return merged  # 返回最终语音段列表


def _load_asr_model() -> WhisperModel:
    """加载Whisper ASR模型并返回。"""  # 集中封装模型创建，便于未来改缓存/参数
    return WhisperModel(
        ASR_MODEL,  # 模型名或本地路径
        device=ASR_DEVICE,  # 推理设备
        compute_type=ASR_COMPUTE_TYPE,  # 推理精度/量化方式
    )  # 初始化结束


def transcribe_segments(
    wav_path: Path,  # 输入wav路径（16k单声道）
    speech: List[SpeechSeg],  # VAD得到的语音段列表
    *,  # 下面参数必须用关键字传入，避免位置参数混乱
    language: Optional[str],  # 指定语言（如"zh"），None表示让模型自行处理
    on_progress: Optional[ProgressFn],  # 进度回调函数（可选）
) -> List[AsrSeg]:
    """对每个语音段做ASR，返回带文本的AsrSeg列表。"""  # VAD切段后逐段识别并汇总时间轴
    if not speech:  # 如果没有语音段
        return []  # 直接返回空（避免加载模型）

    x = _read_wav_mono_16k(wav_path)  # 读入完整wav样本数组
    model = _load_asr_model()  # 加载/创建ASR模型

    out: List[AsrSeg] = []  # 用于收集所有识别结果段
    for idx, seg in enumerate(speech):  # 遍历每个语音段（带索引用于进度）
        # slice by samples  # 把毫秒时间换成样本索引切片
        s0 = int(seg.start_ms * TARGET_SR / 1000)  # 起始毫秒→起始样本索引
        s1 = int(seg.end_ms * TARGET_SR / 1000)  # 结束毫秒→结束样本索引
        s0 = max(0, min(len(x), s0))  # 把s0限制在[0, len(x)]范围内
        s1 = max(0, min(len(x), s1))  # 把s1限制在[0, len(x)]范围内
        if s1 <= s0:  # 如果切片区间无效或为空
            continue  # 跳过该段

        clip = x[s0:s1]  # 取出该语音段对应的音频片段（numpy数组）
        segments, info = model.transcribe(  # 对片段做转写，返回分段结果与信息
            clip,  # 输入音频样本（float32）
            language=language,  # 指定语言
            vad_filter=False,  # 不启用whisper内置VAD（因为外面已经做了VAD）
            beam_size=1,  # beam=1更快（贪心），更大可能更准但更慢
            condition_on_previous_text=False,  # 不用上一段文本作为条件，避免跨段串扰
        )

        pct = 20 + int(60 * (idx + 1) / max(1, len(speech)))  # 计算进度：把ASR阶段映射到20~80
        _prog(on_progress, pct, f"asr {idx+1}/{len(speech)}")  # 回调汇报：当前识别到第几段

        for s in segments:  # 遍历whisper返回的小段（相对clip的时间）
            start_ms = seg.start_ms + int(float(s.start) * 1000)  # 相对秒→毫秒，并加上语音段全局起点
            end_ms = seg.start_ms + int(float(s.end) * 1000)  # 同上计算全局结束毫秒
            text = (s.text or "").strip()  # 取文本并去空白；None则当空串
            if not text:  # 如果没文本（空段）
                continue  # 跳过
            out.append(AsrSeg(start_ms=start_ms, end_ms=max(end_ms, start_ms + 1), text=text))  # 保存结果（end至少比start大1）

    out.sort(key=lambda t: (t.start_ms, t.end_ms))  # 按时间排序，确保后续合并逻辑稳定
    return out  # 返回所有ASR段


def _ends_with_punct(t: str) -> bool:
    """判断文本是否以句末标点结尾用于断句。"""  # 帮助merge时决定是否在标点处切分
    t = (t or "").strip()  # 防None并去首尾空白
    if not t:  # 如果空字符串
        return False  # 不算句末
    return t[-1] in PUNCT_END  # 判断最后一个字符是否在标点集合里


def merge_asr_to_chunks(asr: List[AsrSeg]) -> List[AsrSeg]:
    """把细粒度ASR段合并成较大chunk（控制时长、标点断句、字数上限）。"""  # 生成适合入库/向量化的段落粒度
    if not asr:  # 如果没有ASR段
        return []  # 返回空

    chunks: List[AsrSeg] = []  # 合并后的chunk列表
    cur_start = asr[0].start_ms  # 当前chunk起点：从第一段开始
    cur_end = asr[0].end_ms  # 当前chunk终点：从第一段结束
    buf: List[str] = [asr[0].text]  # 当前chunk的文本缓冲区（逐段累积）

    def flush(force: bool = False) -> None:
        """把buf里的文本与时间范围写成一个chunk并清空buf。"""  # 把累积内容落盘到chunks
        nonlocal cur_start, cur_end, buf  # 声明使用外层变量并允许修改
        txt = " ".join([b.strip() for b in buf if b.strip()]).strip()  # 拼接buf文本（用空格连接），并清理空串
        if not txt:  # 如果拼出来还是空
            buf = []  # 清空buf
            return  # 直接返回
        if len(txt) > MAX_CHARS_PER_CHUNK:  # 如果文本超长
            txt = txt[:MAX_CHARS_PER_CHUNK]  # 截断到上限（避免单段太大）
        chunks.append(AsrSeg(start_ms=cur_start, end_ms=cur_end, text=txt))  # 写入chunk（用当前时间范围）
        buf = []  # 清空buf，为下一chunk准备

    for s in asr[1:]:  # 从第二段开始遍历，尝试加入当前chunk
        next_end = max(cur_end, s.end_ms)  # 未来chunk的结束时间：取最大，保证覆盖
        next_txt = (buf[-1] if buf else "")  # 取当前buf最后一段文本（这里其实没用到，留作可能的调试/扩展）
        span = next_end - cur_start  # 未来chunk的时长（毫秒）（这里后面会重新计算一次）

        buf.append(s.text)  # 追加当前ASR段文本到buf
        cur_end = next_end  # 更新当前chunk的结束时间

        span = cur_end - cur_start  # 重新计算当前chunk时长（毫秒）
        if span >= MAX_CHUNK_MS:  # 如果超过最大chunk时长
            flush(force=True)  # 强制落一个chunk（参数force目前不影响逻辑）
            cur_start = s.start_ms  # 新chunk起点从当前段开始
            cur_end = s.end_ms  # 新chunk终点为当前段结束
            buf = [s.text]  # buf重置为当前段文本
            continue  # 进入下一段

        if span >= MIN_CHUNK_MS and _ends_with_punct(s.text):  # 如果达到最小chunk时长且当前段以句末标点结束
            flush()  # 在标点处切分落chunk
            cur_start = s.start_ms  # 新chunk起点从当前段开始（注意：这会让当前段成为新chunk首段）
            cur_end = s.end_ms  # 新chunk终点为当前段结束
            buf = [s.text]  # buf重置为当前段文本（等于“把切分点段作为下一段开头”）

    if buf:  # 循环结束后，如果buf还有残留文本
        flush(force=True)  # 落最后一个chunk

    chunks.sort(key=lambda t: (t.start_ms, t.end_ms))  # 按时间再排序一遍（保证有序）
    return chunks

def _vs_add(vs: Any, docs: List[Document], ids: List[str]) -> None:
    """向向量库写入文档，兼容add_documents/add_texts两种接口。  适配不同vectorstore实现"""
    if hasattr(vs, "add_documents"):  # 如果vectorstore支持add_documents
        vs.add_documents(docs, ids=ids)  # 直接写入Document列表并指定ids
        return  # 写完返回
    texts = [d.page_content for d in docs]  # 否则提取纯文本列表
    metas = [d.metadata for d in docs]  # 提取metadata列表（与texts一一对应）
    if hasattr(vs, "add_texts"):  # 如果支持add_texts
        vs.add_texts(texts, metadatas=metas, ids=ids)  # 用texts+metadatas写入并指定ids
        return  # 写完返回
    raise RuntimeError("Vectorstore does not support add_documents/add_texts")  # 两种都不支持则报错


def _db_replace_segments(audio_id: str, rows: List[Dict[str, Any]]) -> None:
    """把某audio_id对应的segments整体替换写入数据库（优先原子replace）。"""  # 保证DB里segments与本次chunk一致
    if hasattr(audio_db, "replace_audio_segments"):  # 如果DB层提供replace接口（理想：内部可做事务/原子替换）
        audio_db.replace_audio_segments(audio_id, rows)  # 直接替换
        return  # 完成返回

    if hasattr(audio_db, "delete_audio_segments") and hasattr(audio_db, "insert_audio_segments_bulk"):  # 如果只有delete+bulk insert
        audio_db.delete_audio_segments(audio_id)  # 先删掉该audio_id的旧segments
        audio_db.insert_audio_segments_bulk(rows)  # 再批量插入新segments
        return  # 完成返回

    raise AttributeError("audio_db.replace_audio_segments not found (and no fallback delete/insert found)")  # 两种方案都没有则报错


def run_audio_ingest_pipeline(
    *,  # 强制关键字参数调用，提高可读性与防错
    audio_id: str,  # 音频ID：用于生成segment_id、wav名、入库主键等
    raw_path: Path,  # 原始音频文件路径（可能是mp3/m4a/wav等）
    original_filename: str,  # 原始文件名：写入metadata便于展示
    visibility: str,  # 可见性/权限字段：入库时写入
    language: Optional[str],  # 语言提示：传给ASR
    wav_dir: Path,  # wav输出目录：存转码后的标准wav
    on_progress: Optional[ProgressFn] = None,  # 进度回调：更新任务进度/日志
) -> Dict[str, Any]:
    """完整入库流水线：转码→VAD→ASR→合并chunk→写DB→写向量库。"""  # 函数总体作用：一站式完成音频索引入库
    if not raw_path.exists():  # 如果原始文件不存在
        raise FileNotFoundError(str(raw_path))  # 立刻报错（上层任务应标记失败）

    _prog(on_progress, 1, "start")  # 汇报开始进度：1%

    wav_dir.mkdir(parents=True, exist_ok=True)  # 确保wav输出目录存在
    wav_path = wav_dir / f"{audio_id}.wav"  # 生成标准wav文件路径（用audio_id命名）

    _prog(on_progress, 5, "transcoding")
    transcode_to_wav_16k_mono(raw_path, wav_path)  # 执行转码，得到16k单声道wav

    duration_ms = ffprobe_duration_ms(wav_path)  # 用ffprobe读取wav时长（ms）

    _prog(on_progress, 10, "vad")
    speech = detect_speech_segments(wav_path)  # 做VAD得到“语音段”列表

    _prog(on_progress, 15, f"vad segments={len(speech)}")
    asr = transcribe_segments(wav_path, speech, language=language, on_progress=on_progress)  # 对语音段做ASR并持续汇报进度

    _prog(on_progress, 85, f"asr segments={len(asr)}")
    chunks = merge_asr_to_chunks(asr)  # 把ASR小段合并成更适合入库的chunk

    _prog(on_progress, 88, f"chunks={len(chunks)}")

    rows: List[Dict[str, Any]] = []  # DB要写入的行列表（每行一个chunk）
    docs: List[Document] = []  # 向量库要写入的Document列表
    ids: List[str] = []  # 每个chunk对应的唯一id列表（与docs一一对应）

    for i, c in enumerate(chunks):  # 遍历每个chunk并构造DB行/向量文档
        seg_id = f"{audio_id}:{i}"  # 生成segment_id：audio_id + 索引（保证同音频内唯一）
        start_ms = int(c.start_ms)  # chunk起始毫秒（转int确保可序列化）
        end_ms = int(c.end_ms)  # chunk结束毫秒
        text = (c.text or "").strip()  # chunk文本（防None并去空白）

        rows.append({
            "audio_id": audio_id,  # 音频ID
            "segment_idx": i,  # 段序号（从0开始）
            "segment_id": seg_id,  # 段唯一ID
            "start_ms": start_ms,  # 起始时间
            "end_ms": end_ms,  # 结束时间
            "text": text,  # 文本内容
            "visibility": visibility,  # 可见性/权限
        })

        meta = {  # 构造向量库metadata（检索时可用于过滤/展示）
            "audio_id": audio_id,
            "segment_id": seg_id,
            "start_ms": start_ms,
            "end_ms": end_ms,
            "visibility": visibility,
            "original_filename": original_filename,  # 原始文件名（前端展示友好）
        }
        docs.append(Document(page_content=text, metadata=meta))  # 构造Document：文本+metadata
        ids.append(seg_id)  # 记录对应id（与docs顺序一致）

    _prog(on_progress, 90, "write db segments")
    _db_replace_segments(audio_id, rows)  # 把segments写入数据库（替换旧数据）

    _prog(on_progress, 93, "write vectors")
    vs = get_audio_vs()  # 获取向量库实例（由依赖注入/配置决定后端）
    _vs_add(vs, docs, ids)  # 写入向量库（适配不同接口）

    _prog(on_progress, 100, "done")

    return {  # 返回流水线结果
        "audio_id": audio_id,  # 音频ID
        "duration_ms": int(duration_ms),  # 音频总时长（ms）
        "segments": int(len(chunks)),  # 最终chunk数量（入库段数）
        "language": language,  # 使用的语言提示（注意：这里是传入值，不是自动检测结果）
        "wav_path": str(wav_path),  # 转码后的wav路径（便于排查/复用）
    }

