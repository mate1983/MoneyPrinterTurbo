import glob
import itertools
import os
import random
import gc
import shutil
from typing import List
from loguru import logger
from moviepy import (
    AudioFileClip,
    ColorClip,
    CompositeAudioClip,
    CompositeVideoClip,
    ImageClip,
    TextClip,
    VideoFileClip,
    afx,
    concatenate_videoclips,
)
from moviepy.video.tools.subtitles import SubtitlesClip
from PIL import ImageFont

from app.models import const
from app.models.schema import (
    MaterialInfo,
    VideoAspect,
    VideoConcatMode,
    VideoParams,
    VideoTransitionMode, VideoCropMode, VideoEncodePreset, VideoRenderEngine,
)
from app.services.utils import video_effects
from app.utils import utils

# 在 video.py 的开头添加
import os
import sys
import subprocess
import logging

logger = logging.getLogger(__name__)

# 强制设置FFMPEG_BINARY
def setup_ffmpeg():
    """配置FFmpeg路径"""
    # 如果已设置，直接使用
    if "FFMPEG_BINARY" in os.environ:
        logger.info(f"使用已配置的FFMPEG_BINARY: {os.environ['FFMPEG_BINARY']}")
        return

    # 尝试找到可用的ffmpeg
    candidates = [
        '/usr/bin/ffmpeg',
        '/bin/ffmpeg',
        '/usr/local/bin/ffmpeg',
        'ffmpeg'
    ]

    for candidate in candidates:
        try:
            result = subprocess.run(
                [candidate, '-version'],
                capture_output=True,
                text=True,
                timeout=2
            )
            if result.returncode == 0:
                os.environ["FFMPEG_BINARY"] = candidate
                logger.info(f"✅ 设置FFMPEG_BINARY为: {candidate}")
                logger.info(f"   FFmpeg版本: {result.stdout.split('\\n')[0]}")
                return
        except Exception as e:
            logger.debug(f"候选路径 {candidate} 不可用: {e}")
            continue

    # 如果都没找到，使用默认
    os.environ["FFMPEG_BINARY"] = "/usr/bin/ffmpeg"
    logger.warning(f"⚠️  强制设置FFMPEG_BINARY为: /usr/bin/ffmpeg")


# 调用设置函数
setup_ffmpeg()

class SubClippedVideoClip:
    def __init__(self, file_path, start_time=None, end_time=None, width=None, height=None, duration=None):
        self.file_path = file_path
        self.start_time = start_time
        self.end_time = end_time
        self.width = width
        self.height = height
        if duration is None:
            self.duration = end_time - start_time
        else:
            self.duration = duration

    def __str__(self):
        return f"SubClippedVideoClip(file_path={self.file_path}, start_time={self.start_time}, end_time={self.end_time}, duration={self.duration}, width={self.width}, height={self.height})"


audio_codec = "aac"
video_codec = "libx264"
fps = 30

def close_clip(clip):
    if clip is None:
        return
        
    try:
        # close main resources
        if hasattr(clip, 'reader') and clip.reader is not None:
            clip.reader.close()
            
        # close audio resources
        if hasattr(clip, 'audio') and clip.audio is not None:
            if hasattr(clip.audio, 'reader') and clip.audio.reader is not None:
                clip.audio.reader.close()
            del clip.audio
            
        # close mask resources
        if hasattr(clip, 'mask') and clip.mask is not None:
            if hasattr(clip.mask, 'reader') and clip.mask.reader is not None:
                clip.mask.reader.close()
            del clip.mask
            
        # handle child clips in composite clips
        if hasattr(clip, 'clips') and clip.clips:
            for child_clip in clip.clips:
                if child_clip is not clip:  # avoid possible circular references
                    close_clip(child_clip)
            
        # clear clip list
        if hasattr(clip, 'clips'):
            clip.clips = []
            
    except Exception as e:
        logger.error(f"failed to close clip: {str(e)}")
    
    del clip
    gc.collect()

def delete_files(files: List[str] | str):
    if isinstance(files, str):
        files = [files]
        
    for file in files:
        try:
            os.remove(file)
        except:
            pass

def get_bgm_file(bgm_type: str = "random", bgm_file: str = ""):
    if not bgm_type:
        return ""

    if bgm_file and os.path.exists(bgm_file):
        return bgm_file

    if bgm_type == "random":
        suffix = "*.mp3"
        song_dir = utils.song_dir()
        files = glob.glob(os.path.join(song_dir, suffix))
        return random.choice(files)

    return ""

def get_ffmpeg_encode_args(preset: VideoEncodePreset) -> list[str]:
    if preset == VideoEncodePreset.quality:
        return [
            "-c:v", "libx264",
            "-crf", "18",
            "-preset", "veryfast",
            "-pix_fmt", "yuv420p",
            "-profile:v", "high",
            "-level", "4.1",
        ]

    if preset == VideoEncodePreset.speed:
        return [
            "-c:v", "libx264",
            "-crf", "20",
            "-preset", "superfast",
            "-pix_fmt", "yuv420p",
        ]

    if preset == VideoEncodePreset.gpu:
        return [
            "-c:v", "h264_nvenc",
            "-preset", "p4",
            "-rc", "vbr",
            "-cq", "19",
        ]

    raise ValueError(f"Unknown preset: {preset}")
def build_vf_filter(crop_mode: VideoCropMode, w: int, h: int) -> str:
    """
    构建 FFmpeg -vf 滤镜
    """
    if crop_mode == VideoCropMode.none:
        return f"scale={w}:{h}"

    if crop_mode == VideoCropMode.fit:
        # 等比缩放 + 黑边
        return (
            f"scale=w={w}:h={h}:force_original_aspect_ratio=decrease,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:black"
        )

    if crop_mode == VideoCropMode.fill:
        # 强制拉伸（可能变形）
        return f"scale={w}:{h}"

    if crop_mode == VideoCropMode.smart:
        # 等比填满 + 居中裁剪
        return (
            f"scale=w={w}:h={h}:force_original_aspect_ratio=increase,"
            f"crop={w}:{h}"
        )

    if crop_mode == VideoCropMode.zoom:
        # 先放大再裁（和 smart 类似，但语义区分）
        return (
            f"scale=w={w}:h={h}:force_original_aspect_ratio=increase,"
            f"crop={w}:{h}"
        )

    raise ValueError(f"Unknown crop mode: {crop_mode}")


def combine_videos(
    combined_video_path: str,
    video_paths: List[str],
    audio_file: str,
    video_aspect: VideoAspect | None = None,
    video_width: int | None = None,
    video_height: int | None = None,
    max_clip_duration: int = 5,
    crop_mode: VideoCropMode = VideoCropMode.fit,
    fps: int = 30,
    video_transition_mode=None,  # ✅ 吃掉但暂不实现
    **kwargs,                    # ✅ 防止将来再炸
):
    """
    FFmpeg 极速拼接版本（兼容旧调用）
    """

    # ---------- 1. 解析分辨率 ----------
    if video_aspect is not None:
        aspect = VideoAspect(video_aspect)
        video_width, video_height = aspect.to_resolution()

    if not video_width or not video_height:
        raise ValueError("video_width / video_height 未指定")

    os.makedirs(os.path.dirname(combined_video_path), exist_ok=True)
    workdir = os.path.dirname(combined_video_path)

    vf = build_vf_filter(crop_mode, video_width, video_height)
    temp_clips = []

    # ---------- 2. 裁剪 + 统一规格 ----------
    for i, src in enumerate(video_paths):
        out = os.path.join(workdir, f"clip_{i}.mp4")

        cmd = [
            "ffmpeg", "-y",
            "-i", src,
            "-t", str(max_clip_duration),
            "-vf", vf,
            "-r", str(fps),
            "-an",
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-pix_fmt", "yuv420p",
            out,
        ]

        subprocess.run(cmd, check=True)
        temp_clips.append(out)

    if not temp_clips:
        raise RuntimeError("未生成任何视频片段")

    # ---------- 3. concat ----------
    concat_txt = os.path.join(workdir, "concat.txt")
    with open(concat_txt, "w", encoding="utf-8") as f:
        for p in temp_clips:
            f.write(f"file '{os.path.abspath(p)}'\n")

    merged = os.path.join(workdir, "merged.mp4")
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", concat_txt,
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-pix_fmt", "yuv420p",
            merged,
        ],
        check=True,
    )

    # ---------- 4. 合成音频 ----------
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-i", merged,
            "-i", audio_file,
            "-c:v", "copy",
            "-c:a", "aac",
            "-shortest",
            combined_video_path,
        ],
        check=True,
    )

    # ---------- 5. 清理 ----------
    for p in temp_clips:
        os.remove(p)
    os.remove(concat_txt)
    os.remove(merged)

    return combined_video_path

def wrap_text(text, max_width, font="Arial", fontsize=60):
    # Create ImageFont
    font = ImageFont.truetype(font, fontsize)

    def get_text_size(inner_text):
        inner_text = inner_text.strip()
        left, top, right, bottom = font.getbbox(inner_text)
        return right - left, bottom - top

    width, height = get_text_size(text)
    if width <= max_width:
        return text, height

    processed = True

    _wrapped_lines_ = []
    words = text.split(" ")
    _txt_ = ""
    for word in words:
        _before = _txt_
        _txt_ += f"{word} "
        _width, _height = get_text_size(_txt_)
        if _width <= max_width:
            continue
        else:
            if _txt_.strip() == word.strip():
                processed = False
                break
            _wrapped_lines_.append(_before)
            _txt_ = f"{word} "
    _wrapped_lines_.append(_txt_)
    if processed:
        _wrapped_lines_ = [line.strip() for line in _wrapped_lines_]
        result = "\n".join(_wrapped_lines_).strip()
        height = len(_wrapped_lines_) * height
        return result, height

    _wrapped_lines_ = []
    chars = list(text)
    _txt_ = ""
    for word in chars:
        _txt_ += word
        _width, _height = get_text_size(_txt_)
        if _width <= max_width:
            continue
        else:
            _wrapped_lines_.append(_txt_)
            _txt_ = ""
    _wrapped_lines_.append(_txt_)
    result = "\n".join(_wrapped_lines_).strip()
    height = len(_wrapped_lines_) * height
    return result, height


def generate_video(
    video_path: str,
    audio_path: str,
    subtitle_path: str,
    output_file: str,
    params: VideoParams,
):
    aspect = VideoAspect(params.video_aspect)
    video_width, video_height = aspect.to_resolution()

    logger.info(f"generating video: {video_width} x {video_height}")
    logger.info(f"  ① video: {video_path}")
    logger.info(f"  ② audio: {audio_path}")
    logger.info(f"  ③ subtitle: {subtitle_path}")
    logger.info(f"  ④ output: {output_file}")

    # https://github.com/harry0703/MoneyPrinterTurbo/issues/217
    # PermissionError: [WinError 32] The process cannot access the file because it is being used by another process: 'final-1.mp4.tempTEMP_MPY_wvf_snd.mp3'
    # write into the same directory as the output file
    output_dir = os.path.dirname(output_file)

    font_path = ""
    if params.subtitle_enabled:
        if not params.font_name:
            params.font_name = "STHeitiMedium.ttc"
        font_path = os.path.join(utils.font_dir(), params.font_name)
        if os.name == "nt":
            font_path = font_path.replace("\\", "/")

        logger.info(f"  ⑤ font: {font_path}")

    def create_text_clip(subtitle_item):
        params.font_size = int(params.font_size)
        params.stroke_width = int(params.stroke_width)
        phrase = subtitle_item[1]
        max_width = video_width * 0.9
        wrapped_txt, txt_height = wrap_text(
            phrase, max_width=max_width, font=font_path, fontsize=params.font_size
        )
        interline = int(params.font_size * 0.25)
        size=(int(max_width), int(txt_height + params.font_size * 0.25 + (interline * (wrapped_txt.count("\n") + 1))))

        _clip = TextClip(
            text=wrapped_txt,
            font=font_path,
            font_size=params.font_size,
            color=params.text_fore_color,
            bg_color=params.text_background_color,
            stroke_color=params.stroke_color,
            stroke_width=params.stroke_width,
            # interline=interline,
            # size=size,
        )
        duration = subtitle_item[0][1] - subtitle_item[0][0]
        _clip = _clip.with_start(subtitle_item[0][0])
        _clip = _clip.with_end(subtitle_item[0][1])
        _clip = _clip.with_duration(duration)
        if params.subtitle_position == "bottom":
            _clip = _clip.with_position(("center", video_height * 0.95 - _clip.h))
        elif params.subtitle_position == "top":
            _clip = _clip.with_position(("center", video_height * 0.05))
        elif params.subtitle_position == "custom":
            # Ensure the subtitle is fully within the screen bounds
            margin = 10  # Additional margin, in pixels
            max_y = video_height - _clip.h - margin
            min_y = margin
            custom_y = (video_height - _clip.h) * (params.custom_position / 100)
            custom_y = max(
                min_y, min(custom_y, max_y)
            )  # Constrain the y value within the valid range
            _clip = _clip.with_position(("center", custom_y))
        else:  # center
            _clip = _clip.with_position(("center", "center"))
        return _clip

    video_clip = VideoFileClip(video_path).without_audio()
    audio_clip = AudioFileClip(audio_path).with_effects(
        [afx.MultiplyVolume(params.voice_volume)]
    )

    def make_textclip(text):
        return TextClip(
            text=text,
            font=font_path,
            font_size=params.font_size,
        )

    if subtitle_path and os.path.exists(subtitle_path):
        sub = SubtitlesClip(
            subtitles=subtitle_path, encoding="utf-8", make_textclip=make_textclip
        )
        text_clips = []
        for item in sub.subtitles:
            clip = create_text_clip(subtitle_item=item)
            text_clips.append(clip)
        video_clip = CompositeVideoClip([video_clip, *text_clips])

    bgm_file = get_bgm_file(bgm_type=params.bgm_type, bgm_file=params.bgm_file)
    if bgm_file:
        try:
            bgm_clip = AudioFileClip(bgm_file).with_effects(
                [
                    afx.MultiplyVolume(params.bgm_volume),
                    afx.AudioFadeOut(3),
                    afx.AudioLoop(duration=video_clip.duration),
                ]
            )
            audio_clip = CompositeAudioClip([audio_clip, bgm_clip])
        except Exception as e:
            logger.error(f"failed to add bgm: {str(e)}")

    video_clip = video_clip.with_audio(audio_clip)
    video_clip.write_videofile(
        output_file,
        audio_codec=audio_codec,
        temp_audiofile_path=output_dir,
        threads=params.n_threads or 2,
        logger=None,
        fps=fps,
    )
    video_clip.close()
    del video_clip


def preprocess_video(materials: List[MaterialInfo], clip_duration=4):
    for material in materials:
        if not material.url:
            continue

        ext = utils.parse_extension(material.url)
        try:
            clip = VideoFileClip(material.url)
        except Exception:
            clip = ImageClip(material.url)

        width = clip.size[0]
        height = clip.size[1]
        if width < 480 or height < 480:
            logger.warning(f"low resolution material: {width}x{height}, minimum 480x480 required")
            continue

        if ext in const.FILE_TYPE_IMAGES:
            logger.info(f"processing image: {material.url}")
            # Create an image clip and set its duration to 3 seconds
            clip = (
                ImageClip(material.url)
                .with_duration(clip_duration)
                .with_position("center")
            )
            # Apply a zoom effect using the resize method.
            # A lambda function is used to make the zoom effect dynamic over time.
            # The zoom effect starts from the original size and gradually scales up to 120%.
            # t represents the current time, and clip.duration is the total duration of the clip (3 seconds).
            # Note: 1 represents 100% size, so 1.2 represents 120% size.
            zoom_clip = clip.resized(
                lambda t: 1 + (clip_duration * 0.03) * (t / clip.duration)
            )

            # Optionally, create a composite video clip containing the zoomed clip.
            # This is useful when you want to add other elements to the video.
            final_clip = CompositeVideoClip([zoom_clip])

            # Output the video to a file.
            video_file = f"{material.url}.mp4"
            final_clip.write_videofile(video_file, fps=30, logger=None)
            close_clip(clip)
            material.url = video_file
            logger.success(f"image processed: {video_file}")
    return materials